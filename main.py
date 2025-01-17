# Copyright (c) Aishwarya Kamath & Nicolas Carion. Licensed under the Apache License 2.0. All Rights Reserved
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import argparse
import datetime
import json
import os
import random
import time
from collections import namedtuple
from copy import deepcopy
from functools import partial
from pathlib import Path
from xml.sax.handler import feature_string_interning
import psutil

import numpy as np
import torch
import torch.utils
from torch.utils.data import ConcatDataset, DataLoader, DistributedSampler

import util.dist as dist
import util.misc as utils
from datasets import build_dataset, get_coco_api_from_dataset
from datasets.coco_eval import CocoEvaluator
from datasets.flickr_eval import FlickrEvaluator, FlickrCaptionEvaluator
from datasets.refexp import RefExpEvaluator
from engine import evaluate, train_one_epoch
from models import build_model
from models.postprocessors import build_postprocessors

import colossalai
from colossalai.core import global_context as gpc
from colossalai.logging import disable_existing_loggers, get_dist_logger
from colossalai.utils import save_checkpoint
from colossalai.zero.init_ctx import ZeroInitContext
from colossalai.zero.shard_utils import BucketTensorShardStrategy, TensorShardStrategy


from colossalai.gemini import GeminiManager
from colossalai.gemini.chunk import ChunkManager
from colossalai.utils.model.colo_init_context import ColoInitContext
from colossalai.utils import get_current_device
from colossalai.nn.parallel import ZeroDDP
from colossalai.nn.optimizer import HybridAdam
from colossalai.zero import ZeroOptimizer
from colossalai.tensor import ProcessGroup


import deepspeed

# You may need this with fp16, for BatchEncoder in huggingface not supports fp16
import logging
logging.disable(logging.WARNING)


def get_args_parser():
    parser = argparse.ArgumentParser("Set transformer detector", add_help=False)

    # Dataset specific
    parser.add_argument("--dataset_config", default=None, required=True)
    parser.add_argument("--unitab_pretrain", action="store_true", help="Whether to do the simvlm like split text pretrain; now mainly use in dataloader IO text generation")
    parser.add_argument("--pretrain_seqcrop", default="mixed", type=str, help="How to crop the sequence during unitab pretraining. first, rand, \
        , grounding, or mixed (first+grounding)")
    parser.add_argument("--do_caption", action="store_true", help="Whether to do text generation")
    parser.add_argument("--do_flickrgrounding", action="store_true", help="a high level key for the flickr grounding experiments; \
        will need keys --dataset_config configs/flickr.json, without --do_caption --no_detection")
    parser.add_argument("--no_detection", action="store_true", help="Whether to train the detector")
    parser.add_argument(
        "--combine_datasets", nargs="+", help="List of datasets to combine for training", default=["flickr"]
    )
    parser.add_argument(
        "--combine_datasets_val", nargs="+", help="List of datasets to combine for eval", default=["flickr"]
    )

    parser.add_argument("--coco_path", type=str, default="")
    parser.add_argument("--vg_img_path", type=str, default="")
    parser.add_argument("--vg_ann_path", type=str, default="")

    # Training hyper-parameters
    parser.add_argument("--lr", default=1e-4, type=float)
    parser.add_argument("--lr_backbone", default=1e-5, type=float)
    parser.add_argument("--text_encoder_lr", default=5e-5, type=float)
    parser.add_argument("--batch_size", default=2, type=int)
    parser.add_argument("--weight_decay", default=1e-4, type=float)
    parser.add_argument("--epochs", default=40, type=int)
    parser.add_argument("--lr_drop", default=35, type=int)
    parser.add_argument("--optimizer", default="adam", type=str)
    parser.add_argument("--clip_max_norm", default=0.1, type=float, help="gradient clipping max norm")
    parser.add_argument(
        "--eval_skip",
        default=1,
        type=int,
        help='do evaluation every "eval_skip" frames',
    )

    parser.add_argument(
        "--schedule",
        default="linear_with_warmup",
        type=str,
        choices=("step", "multistep", "linear_with_warmup", "all_linear_with_warmup"),
    )
    parser.add_argument("--ema", action="store_true")
    parser.add_argument("--ema_decay", type=float, default=0.9998)
    parser.add_argument("--fraction_warmup_steps", default=0.01, type=float, help="Fraction of total number of steps")

    # Model parameters
    parser.add_argument(
        "--frozen_weights",
        type=str,
        default=None,
        help="Path to the pretrained model. If set, only the mask head will be trained",
    )

    parser.add_argument(
        "--freeze_text_encoder", action="store_true", help="Whether to freeze the weights of the text encoder"
    )

    parser.add_argument(
        "--text_encoder_type",
        default="roberta-base",
        choices=("roberta-base"),
    )

    # Backbone
    parser.add_argument(
        "--backbone",
        default="resnet101",
        type=str,
        help="Name of the convolutional backbone",
    )
    parser.add_argument(
        "--position_embedding",
        default="sine",
        type=str,
        choices=("sine", "learned"),
        help="Type of positional embedding to use on top of the image features",
    )

    # Transformer
    parser.add_argument(
        "--enc_layers",
        default=6,
        type=int,
        help="Number of encoding layers in the transformer",
    )
    parser.add_argument(
        "--dec_layers",
        default=6,
        type=int,
        help="Number of decoding layers in the transformer",
    )
    parser.add_argument(
        "--dim_feedforward",
        default=2048,
        type=int,
        help="Intermediate size of the feedforward layers in the transformer blocks",
    )
    parser.add_argument(
        "--hidden_dim",
        default=256,
        type=int,
        help="Size of the embeddings (dimension of the transformer)",
    )
    parser.add_argument("--dropout", default=0.1, type=float, help="Dropout applied in the transformer")
    parser.add_argument(
        "--nheads",
        default=8,
        type=int,
        help="Number of attention heads inside the transformer's attentions",
    )
    parser.add_argument("--max_decoding_step", default=256, type=int, help="max_decoding_step for text generation")
    parser.add_argument("--num_queries", default=200, type=int, help="Number of object tokens")
    parser.add_argument("--pre_norm", action="store_true")

    # Run specific
    parser.add_argument("--test", action="store_true", help="Whether to run evaluation on val or test set")
    parser.add_argument("--test_type", type=str, default="test", choices=("testA", "testB", "test"))
    parser.add_argument("--output-dir", default="", help="path where to save, empty for no saving")
    parser.add_argument("--device", default="cuda", help="device to use for training / testing")
    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--resume", default="", help="resume from checkpoint")
    parser.add_argument("--load", default="", help="resume from checkpoint")
    parser.add_argument("--start-epoch", default=0, type=int, metavar="N", help="start epoch")
    parser.add_argument("--eval", action="store_true", help="Only run evaluation")
    parser.add_argument("--num_workers", default=5, type=int)

    # Distributed training parameters
    parser.add_argument("--dist-url", default="env://", help="url used to set up distributed training")
    
    # Distributed training parameters for colossalai
    parser.add_argument('--colossalai_config', default=None, type=str, help='path to the config file')
    parser.add_argument("--distributed", action="store_true", help="set up distributed training mode or not")
    parser.add_argument("--from_colossalai", action="store_true", help="luanch from colossalai or not")
    parser.add_argument('--host', type=str, default='127.0.0.1', help='the master address for distributed training')
    parser.add_argument('--port', type=int, default=29500, help='the master port for distributed training')
    parser.add_argument('--world_size', type=int, default=2, help='world size for distributed training')
    parser.add_argument('--rank', type=int, default=0, help='rank for the default process group')
    parser.add_argument('--local_rank', type=int, default=0, help='local rank on the node')
    parser.add_argument('--backend', type=str, default='nccl', help='backend for distributed communication')

    parser.add_argument("--mem_cap", type=int, default=0, help="use mem cap in GPU, 0 means no memory cap")
    parser.add_argument("--use_colo_zero", action='store_true', help="use ZeRO of ColossalAI")

    parser.add_argument('--from_deepspeed', action='store_true', default=None, help='whether use deepspeed or not')
    parser.add_argument("--deepspeed_config", type=str, default=None, help='path to deepspeed config file')
    return parser

def get_cpu_mem():
    return psutil.Process().memory_info().rss / 1024**2

def get_gpu_mem():
    return torch.cuda.memory_allocated() / 1024**2


def get_mem_info(prefix=''):
    return f'{prefix}GPU memory usage: {get_gpu_mem():.2f} MB, CPU memory usage: {get_cpu_mem():.2f} MB'


def main(args):
    # Init distributed mode

    if args.from_colossalai:
        print("init distributed mode from colossalai")
        if args.distributed:
            colossalai.launch_from_torch(config=args.colossalai_config)
        else:
            colossalai.launch(config=args.colossalai_config,
                        rank=args.rank,
                        world_size=args.world_size,
                        host=args.host,
                        port=args.port,
                        backend=args.backend)

    elif args.distributed:
        print("init distributed mode from torch")
        dist.init_distributed_mode(args)

    logger = get_dist_logger('cai')
    # cap memory to 
    if args.mem_cap > 0:
        def colo_memory_cap(size_in_GB):
            from colossalai.utils import colo_set_process_memory_fraction, colo_device_memory_capacity
            from colossalai.utils import get_current_device
            cuda_capacity = colo_device_memory_capacity(get_current_device())
            if size_in_GB * (1024**3) < cuda_capacity:
                colo_set_process_memory_fraction(size_in_GB * (1024**3) / cuda_capacity)
                print("Memory Capping, Using {} GB of GPU memory".format(size_in_GB))

        colo_memory_cap(args.mem_cap)

    # Update dataset specific configs
    if args.dataset_config is not None:
        # https://stackoverflow.com/a/16878364
        d = vars(args)
        with open(args.dataset_config, "r") as f:
            cfg = json.load(f)
        d.update(cfg)

    print("git:\n  {}\n".format(utils.get_sha()))

    print(args)

    device = torch.device(args.device)
    output_dir = Path(args.output_dir)

    # fix the seed for reproducibility
    seed = args.seed + dist.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    #torch.set_deterministic(True)

    # Build the model
    if args.use_colo_zero:
        PLACEMENT_POLICY = 'cpu'
        with ColoInitContext(device=get_current_device()):
            model, criterion, weight_dict = build_model(args)
        chunk_size = ChunkManager.search_chunk_size(model, 64 * 1024**2, 32)
        pg = ProcessGroup()
        chunk_manager = ChunkManager(chunk_size, pg, enable_distributed_storage=True,
                                    init_device=GeminiManager.get_default_device(PLACEMENT_POLICY))
        gemini_manager = GeminiManager(PLACEMENT_POLICY, chunk_manager)
        model = ZeroDDP(model, gemini_manager)
        logger.info(get_mem_info(prefix='After init model, '), ranks=[0])
        logger.info(chunk_manager, ranks=[0])
        
    else:
        model, criterion, weight_dict = build_model(args)
        model.to(device)



    # Get a copy of the model for exponential moving averaged version of the model
    model_ema = deepcopy(model) if args.ema else None
    # ctx = ZeroInitContext(target_device=torch.cuda.current_device(),
    #                     shard_strategy=gpc.config.zero.model_config.shard_strategy,
    #                     shard_param=True)
    # with ctx:
    model_without_ddp = model
    if args.distributed and not args.from_colossalai:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True, broadcast_buffers=False)
        model_without_ddp = model.module
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print("number of params:", n_parameters)


    # Set up optimizers
    param_dicts = [
        {
            "params": [
                p
                for n, p in model_without_ddp.named_parameters()
                if "backbone" not in n and "text_encoder" not in n and p.requires_grad
            ]
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if "backbone" in n and p.requires_grad],
            "lr": args.lr_backbone,
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if "text_encoder" in n and p.requires_grad],
            "lr": args.text_encoder_lr,
        },
    ]

    if args.use_colo_zero:
        assert args.optimizer == "adam", "use_colo_zero can only be used with adam optimizer"
        optimizer = HybridAdam(model.parameters(), lr=1e-3)
        optimizer = ZeroOptimizer(optimizer, model, initial_scale=2**5)
        logger.info(get_mem_info(prefix='After init optim, '), ranks=[0])
    else:
        if args.optimizer == "sgd":
            optimizer = torch.optim.SGD(param_dicts, lr=args.lr, momentum=0.9, weight_decay=args.weight_decay)
        elif args.optimizer in ["adam", "adamw"]:
            optimizer = torch.optim.AdamW(param_dicts, lr=args.lr, weight_decay=args.weight_decay)
        else:
            raise RuntimeError(f"Unsupported optimizer {args.optimizer}")

    # Train dataset
    if len(args.combine_datasets) == 0 and not args.eval:
        raise RuntimeError("Please provide at least one training dataset")

    dataset_train, sampler_train, data_loader_train = None, None, None
    if not args.eval:
        #### temporal solution for update refexp_dataset_name and GT_type for multi-task finetuning
        if type(args.refexp_dataset_name)==type([]):
            gttype_cache, refexpname_cache = args.GT_type, args.refexp_dataset_name
            flickr_img_path_cache, coco_path_cache = args.flickr_img_path, args.coco_path
            dataset_list = []
            for ii in range(len(args.combine_datasets)):
                name, gttype, refexpname = args.combine_datasets[ii], gttype_cache[ii], refexpname_cache[ii]
                args.GT_type = gttype
                args.refexp_dataset_name = refexpname
                args.flickr_img_path = flickr_img_path_cache[ii]
                args.coco_path = coco_path_cache[ii]
                dataset_list.append(build_dataset(name, image_set="train", args=args))
                print(len(dataset_list[-1]),name,args.GT_type,args.refexp_dataset_name)
            dataset_train = ConcatDataset(dataset_list)
            args.GT_type, args.refexp_dataset_name = "merged_karpathy", "refcocog"
            args.flickr_img_path, args.coco_path = "data/Flickr30k/flickr30k_images_split/train", "data/coco"
        else:
            dataset_train = ConcatDataset(
                [build_dataset(name, image_set="train", args=args) for name in args.combine_datasets]
            )

        if args.distributed:
            sampler_train = DistributedSampler(dataset_train)
        else:
            sampler_train = torch.utils.data.RandomSampler(dataset_train)

        batch_sampler_train = torch.utils.data.BatchSampler(sampler_train, args.batch_size, drop_last=True)
        data_loader_train = DataLoader(
            dataset_train,
            batch_sampler=batch_sampler_train,
            drop_last=False,
            collate_fn=partial(utils.collate_fn, False),
            num_workers=args.num_workers,
        )

    # Val dataset
    if len(args.combine_datasets_val) == 0:
        raise RuntimeError("Please provide at leas one validation dataset")

    Val_all = namedtuple(typename="val_data", field_names=["dataset_name", "dataloader", "base_ds", "evaluator_list"])

    #### temporal solution for update refexp_dataset_name and GT_type for multi-task finetuning
    if type(args.refexp_dataset_name)==type([]):
        assert("multitask" in args.dataset_config)
        args.GT_type, args.refexp_dataset_name = "merged_karpathy", "refcocog"
        args.flickr_img_path, args.coco_path = "data/Flickr30k/flickr30k_images_split/train", "data/coco"

    val_tuples = []
    for dset_name in args.combine_datasets_val:
        dset = build_dataset(dset_name, image_set="val", args=args)
        sampler = (
            DistributedSampler(dset, shuffle=False) if args.distributed else torch.utils.data.SequentialSampler(dset)
        )
        dataloader = DataLoader(
            dset,
            args.batch_size,
            sampler=sampler,
            drop_last=False,
            collate_fn=partial(utils.collate_fn, False),
            num_workers=args.num_workers,
        )
        base_ds = get_coco_api_from_dataset(dset)
        val_tuples.append(Val_all(dataset_name=dset_name, dataloader=dataloader, base_ds=base_ds, evaluator_list=None))

    if args.frozen_weights is not None:
        if args.resume.startswith("https"):
            checkpoint = torch.hub.load_state_dict_from_url(args.resume, map_location="cpu", check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location="cpu")
        if "model_ema" in checkpoint and checkpoint["model_ema"] is not None:
            model_without_ddp.detr.load_state_dict(checkpoint["model_ema"], strict=False)
        else:
            model_without_ddp.detr.load_state_dict(checkpoint["model"], strict=False)

        if args.ema:
            model_ema = deepcopy(model_without_ddp)

    # Used for loading weights from another model and starting a training from scratch. Especially useful if
    # loading into a model with different functionality.
    if args.load:
        print("loading from", args.load)
        if args.load.startswith("https"):
            checkpoint = torch.hub.load_state_dict_from_url(args.load, map_location="cpu", check_hash=True)
        else:
            checkpoint = torch.load(args.load, map_location="cpu")
        if "model_ema" in checkpoint:
            model_without_ddp.load_state_dict(checkpoint["model_ema"], strict=False)
        else:
            model_without_ddp.load_state_dict(checkpoint["model"], strict=False)
        if args.ema:
            model_ema = deepcopy(model_without_ddp)

    # Used for resuming training from the checkpoint of a model. Used when training times-out or is pre-empted.
    if args.resume:
        if args.resume.startswith("https"):
            checkpoint = torch.hub.load_state_dict_from_url(args.resume, map_location="cpu", check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location="cpu")
        model_without_ddp.load_state_dict(checkpoint["model"])
        if not args.eval and "optimizer" in checkpoint and "epoch" in checkpoint:
            optimizer.load_state_dict(checkpoint["optimizer"])
            args.start_epoch = checkpoint["epoch"] + 1
        if args.ema:
            if "model_ema" not in checkpoint:
                print("WARNING: ema model not found in checkpoint, resetting to current model")
                model_ema = deepcopy(model_without_ddp)
            else:
                model_ema.load_state_dict(checkpoint["model_ema"])

    def build_evaluator_list(base_ds, dataset_name, do_caption):
        """Helper function to build the list of evaluators for a given dataset"""
        evaluator_list = []
        if "flickr" in dataset_name and do_caption:
            evaluator_list.append(
                FlickrCaptionEvaluator(
                    args.flickr_dataset_path,
                    subset="test" if args.test else "val",
                    merge_boxes=args.GT_type == "merged",
                    exp_id=args.output_dir
                )
            )
        if args.no_detection:
            return evaluator_list
        iou_types = ["bbox"]

        evaluator_list.append(CocoEvaluator(base_ds, tuple(iou_types), useCats=False))
        if "refexp" in dataset_name:
            evaluator_list.append(RefExpEvaluator(base_ds, ("bbox")))
        if "flickr" in dataset_name:
            evaluator_list.append(
                FlickrEvaluator(
                    args.flickr_dataset_path,
                    subset="test" if args.test else "val",
                    merge_boxes=args.GT_type == "merged",
                )
            )
        return evaluator_list


    # Runs only evaluation, by default on the validation set unless --test is passed.
    if args.eval:
        test_stats = {}
        test_model = model_ema if model_ema is not None else model
        for i, item in enumerate(val_tuples):
            evaluator_list = build_evaluator_list(item.base_ds, item.dataset_name, args.do_caption)
            postprocessors = build_postprocessors(args, item.dataset_name)
            item = item._replace(evaluator_list=evaluator_list)
            print(f"Evaluating {item.dataset_name}")
            curr_test_stats = evaluate(
                model=test_model,
                criterion=criterion,
                postprocessors=postprocessors,
                weight_dict=weight_dict,
                data_loader=item.dataloader,
                evaluator_list=item.evaluator_list,
                device=device,
                args=args,
            )
            test_stats.update({item.dataset_name + "_" + k: v for k, v in curr_test_stats.items()})

        log_stats = {
            **{f"test_{k}": v for k, v in test_stats.items()},
            "n_parameters": n_parameters,
        }
        print(log_stats)
        return

    # init colossalai features
    colossalai_engine = None
    if args.from_colossalai:
        colossalai_engine, train_dataloader, test_dataloader, _ = colossalai.initialize(model,
                                                                     optimizer = optimizer,
                                                                     criterion = criterion,
                                                                     train_dataloader = data_loader_train,
                                                                     test_dataloader = val_tuples[0])
                                                                     
        # init colossal logger
        logger = get_dist_logger()

    if args.from_deepspeed:
        deepspeed_engine, optimizer, data_loader_train, _ = deepspeed.initialize(
                                                         model=model,
                                                         optimizer=optimizer,
                                                         config=args.deepspeed_config,
                                                         training_data=dataset_train,
                                                         collate_fn=partial(utils.collate_fn, False),
                                                         )
    

    # Runs training and evaluates after every --eval_skip epochs
    print("Start training")
    start_time = time.time()
    best_metric = 0.0
    for epoch in range(args.start_epoch, args.epochs):
        print(f"Starting epoch {epoch}")
        if args.distributed:
            sampler_train.set_epoch(epoch)
            train_stats = train_one_epoch(
                model=model,
                criterion=criterion,
                data_loader=data_loader_train,
                weight_dict=weight_dict,
                optimizer=optimizer,
                device=device,
                epoch=epoch,
                args=args,
                max_norm=args.clip_max_norm,
                model_ema=model_ema,
                colossalai_engine = colossalai_engine,
                deepspeed_engine = deepspeed_engine,
            )

        logger.info(f"Epoch {epoch} - train loss: {loss:.5}")

        if args.output_dir:
            checkpoint_paths = [output_dir / "checkpoint.pth"]
            # extra checkpoint before LR drop and every 2 epochs
            if (epoch + 1) % args.lr_drop == 0 or (epoch + 1) % 2 == 0:
                checkpoint_paths.append(output_dir / f"checkpoint{epoch:04}.pth")
            for checkpoint_path in checkpoint_paths:
                dist.save_on_master(
                    {
                        "model": model_without_ddp.state_dict(),
                        "model_ema": model_ema.state_dict() if args.ema else None,
                        "optimizer": optimizer.state_dict(),
                        "epoch": epoch,
                        "args": args,
                    },
                    checkpoint_path,
                )

        if epoch % args.eval_skip == 0:
            test_stats = {}
            test_model = model_ema if model_ema is not None else model
            for i, item in enumerate(val_tuples):
                evaluator_list = build_evaluator_list(item.base_ds, item.dataset_name, args.do_caption)
                item = item._replace(evaluator_list=evaluator_list)
                postprocessors = build_postprocessors(args, item.dataset_name)
                print(f"Evaluating {item.dataset_name}")
                curr_test_stats = evaluate(
                    model=test_model,
                    criterion=criterion,
                    postprocessors=postprocessors,
                    weight_dict=weight_dict,
                    data_loader=item.dataloader,
                    evaluator_list=item.evaluator_list,
                    device=device,
                    args=args,
                )
                test_stats.update({item.dataset_name + "_" + k: v for k, v in curr_test_stats.items()})
        else:
            test_stats = {}

        log_stats = {
            **{f"train_{k}": v for k, v in train_stats.items()},
            **{f"test_{k}": v for k, v in test_stats.items()},
            "epoch": epoch,
            "n_parameters": n_parameters,
        }

        # if args.output_dir and dist.is_main_process():
        with (output_dir / "log.txt").open("a") as f:
            f.write(json.dumps(log_stats) + "\n")

        if epoch % args.eval_skip == 0:
            if args.do_caption:
                metric = metric_stats["CIDEr"]
            else:
                metric = np.mean([v[1] for k, v in test_stats.items() if "coco_eval_bbox" in k])

            if args.output_dir and metric > best_metric:
                best_metric = metric
                checkpoint_paths = [output_dir / "BEST_checkpoint.pth"]
                # extra checkpoint before LR drop and every 100 epochs
                for checkpoint_path in checkpoint_paths:
                    # dist.save_on_master(
                    #     {
                    #         "model": model_without_ddp.state_dict(),
                    #         "model_ema": model_ema.state_dict() if args.ema else None,
                    #         "optimizer": optimizer.state_dict(),
                    #         "epoch": epoch,
                    #         "args": args,
                    #     },
                    #     checkpoint_path,
                    # )
                    save_checkpoint(checkpoint_paths, epoch, model)

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print("Training time {}".format(total_time_str))
    disable_existing_loggers()

if __name__ == "__main__":
    parser = argparse.ArgumentParser("UniTAB training and evaluation script", parents=[get_args_parser()])
    args = parser.parse_args()
    args.GT_type = ''   ## updated from json in main()
    args.refexp_dataset_name = ''   ## updated from json in main()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
