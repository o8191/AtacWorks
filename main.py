#!/usr/bin/env python

#
# Copyright (c) 2019, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.
#

# system imports
import logging
import os
import random
import sys
import tempfile
import time

import h5py
import multiprocessing
import numpy as np
import pandas as pd
import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel
from torch.optim import Adam

# module imports
from claragenomics.dl4atac.train.dataset import DatasetTrain, DatasetInfer
from claragenomics.dl4atac.train.evaluate import evaluate
from claragenomics.dl4atac.train.infer import infer
from claragenomics.dl4atac.train.losses import MultiLoss
from claragenomics.dl4atac.train.metrics import BCE, MSE, Recall, Specificity, CorrCoef, AUROC
from claragenomics.dl4atac.train.models import *
from claragenomics.dl4atac.train.train import train
from claragenomics.dl4atac.train.utils import *
from claragenomics.io.bedgraphio import expand_interval, intervals_to_bg, df_to_bedGraph
from claragenomics.io.bigwigio import bedgraph_to_bigwig
from cmd_args import parse_args

# python imports
import warnings
import glob
import math
warnings.filterwarnings("ignore")

# Set up logging
log_formatter = logging.Formatter(
    '%(levelname)s:%(asctime)s:%(name)s] %(message)s')
_logger = logging.getLogger('AtacWorks-main')
_handler = logging.StreamHandler()
_handler.setLevel(logging.INFO)
_handler.setFormatter(log_formatter)
_logger.setLevel(logging.INFO)
_logger.addHandler(_handler)


def set_random_seed(seed):
    """Set random seed for reproducability."""

    if seed is not None and seed > 0:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        mpu.model_parallel_cuda_manual_seed(seed)


def get_losses(task, mse_weight, pearson_weight, gpu):
    """
    Return loss function. 
    Args:
        task : Whether the task is regression or classification or both.
        mse_weight : Mean squared error weight.
        pearson_weight : Pearson weight.
        gpu : Number of gpus.
    Return:
        loss_func : list of loss functions for each task.
    """

    if task == "regression":
        loss_func = MultiLoss(['mse', 'pearsonloss'], [
                              mse_weight, pearson_weight], device=gpu)
    elif task == "classification":
        loss_func = MultiLoss('bce', 1, device=gpu)
    elif task == 'both':  # shouldn't reach here for now
        loss_func = [MultiLoss(['mse', 'pearsonloss'], [mse_weight, pearson_weight], device=gpu),
                     MultiLoss('bce', 1, device=gpu)]
    return loss_func


def get_metrics(task, threshold):
    """
    Get metrics. 
    Args:
        task : Whether the task is regression or classification or both.
        threshold : the threshold for classification.
    Return: #TODO
        metrics_reg :
        metrics_cla :
        best_metric :
    """

    metrics_reg = []
    metrics_cla = []
    best_metric = []
    if task == "regression":
        metrics_reg = [MSE(), CorrCoef()]
        best_metric = metrics_reg[-1]
    elif task == "classification":
        metrics_cla = [BCE(), Recall(threshold),
                       Specificity(threshold), AUROC()]
        best_metric = metrics_cla[-1]
    elif task == 'both':
        metrics_reg = [MSE(), CorrCoef()]
        metrics_cla = [BCE(), Recall(threshold),
                       Specificity(threshold), AUROC()]
        best_metric = metrics_cla[-1]

    return metrics_reg, metrics_cla, best_metric

# build_model now does build, load, distribute in one go


def build_model(model, rank, interval_size, afunc, bn, nblocks,
                nblocks_cla, nfilt, nfilt_cla, width, width_cla,
                dil, dil_cla, field, resume, infer, evaluate,
                weights_path, gpu, distributed):
    myprint("Building model: {} ...".format(
        model), color='yellow', rank=rank)
    # TODO: implement a model dic for model instantiation

    if model == 'unet':  # args.task == 'both'
        model = DenoisingUNet(interval_size=interval_size,
                              afunc=afunc, bn=bn)
    elif model == 'fc2':  # args.task == 'classification'
        model = FC2(interval_size=interval_size)

    elif model == 'resnet':
        model = DenoisingResNet(interval_size=interval_size, afunc=afunc, bn=bn, 
                                num_blocks=nblocks, num_blocks_class=nblocks_cla,
                                out_channels=nfilt, out_channels_class=nfilt_cla,
                                kernel_size=width, kernel_size_class=width_cla,
                                dilation=dil, dilation_class=dil_cla)

    elif model == 'linear':
        model = DenoisingLinear(
            interval_size=interval_size, field=field)

    elif model == 'logistic':
        model = DenoisingLogistic(
            interval_size=interval_size, field=field)

    # TODO: there is a potential problem with loading model on each device like this. keep an eye on torch.load()'s map_location arg
    if resume or infer or evaluate:
        model = load_model(model, weights_path, rank)

    model = model.cuda(gpu)

    if distributed:
        _logger.info('Compiling model in DistributedDataParallel')
        model = DistributedDataParallel(model, device_ids=[gpu])
    elif gpu > 1:
        _logger.info('Compiling model in DataParallel')
        model = nn.DataParallel(
            model, device_ids=list(range(gpus))).cuda()

    myprint("Finished building.", color='yellow', rank=rank)
    return model


def check_intervals(intervals_df, sizes_df, h5_file):
    """
    Function to check intervals file used for inference.
    Args:
        intervals_df(Pandas DataFrame): df with cols chrom, start, end
        sizes_df(Pandas dataframe): df with cols chrom, len
        h5_file(str): path to h5 file to match intervals
    """
    # Length of intervals == length of dataset in h5 file
    with h5py.File(h5_file, 'r') as hf:
        len_data = len(hf['data'])
    assert len_data == intervals_df.shape[0], \
        "Size of infer dataset ({}) doesn't match the intervals file ({})".format(len_data, intervals_df.shape[0])

    # Intervals do not cover chromosomes outside sizes file.
    interval_chroms = set(intervals_df['chrom'])
    sizes_chroms = set(sizes_df['chrom'])
    assert interval_chroms.issubset(sizes_chroms), \
         "Intervals file contains chromosomes not in sizes file ({})".format(interval_chroms.difference(sizes_chroms))

    # Interval bounds do not exceed chromosome lengths
    intervals_sizes = intervals_df.merge(sizes_df, on='chrom')
    excess_intervals = intervals_sizes[intervals_sizes['end'] > intervals_sizes['len']]
    assert len(excess_intervals) == 0, \
          "Intervals exceed chromosome sizes in sizes file ({})".format(excess_intervals)


def train_worker(gpu, ngpu_per_node, args, timers=None):
    # fix random seed so models have the same starting weights
    torch.manual_seed(42)

    args.rank = gpu if args.distributed else 0
    args.gpu = gpu

    torch.cuda.set_device(args.gpu)
    _logger.debug('Rank %s' % str(args.rank))

    if args.distributed:
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)

    # Why is model & optimizer built in spawned function?
    model = build_model(model = args.model, rank = args.rank, interval_size = args.interval_size,
                afunc = args.afunc, bn = args.bn, nblocks = args.nblocks, nblocks_cla = args.nblocks_cla,
                nfilt = args.nfilt, nfilt_cla = args.nfilt_cla, width = args.width, width_cla = args.width_cla,
                dil = args.dil, dil_cla = args.dil_cla, field = args.field, resume = args.resume,infer = args.infer,
                evaluate = args.eval, weights_path = args.weights_path, gpu = args.gpu, distributed = args.distributed)
    optimizer = Adam(model.parameters(), lr=args.lr)
    # TODO: LR schedule

    train_dataset = DatasetTrain(args.train_files)
    train_sampler = None
    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(
            train_dataset)
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.bs, shuffle=(train_sampler is None),
        # collate_fn=custom_collate_train,
        num_workers=args.num_workers, pin_memory=True, sampler=train_sampler,
        drop_last=False
    )

    # TODO: need DatasetVal? Not for now
    val_dataset = DatasetTrain(args.val_files)
    val_sampler = None
    if args.distributed:
        val_sampler = torch.utils.data.distributed.DistributedSampler(
            val_dataset)
    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=args.bs, shuffle=False,
        # collate_fn=custom_collate_train,
        num_workers=args.num_workers, pin_memory=True, sampler=val_sampler,
        drop_last=False
        # drop_last=True # need to drop irregular batch for distributed evaluation due to limitation of dist.all_gather
    )

    loss_func = get_losses(args.task, args.mse_weight, args.pearson_weight, args.gpu)

    current_best = None
    for epoch in range(args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        train(rank=args.rank, gpu=args.gpu, task=args.task, model=model, train_loader=train_loader,
              loss_func=loss_func, optimizer=optimizer, epoch=epoch, epochs=args.epochs,
              clip_grad=args.clip_grad, print_freq=args.print_freq, pad=args.pad,
              distributed=args.distributed, world_size=args.world_size, transform=args.transform)

        if epoch % args.eval_freq == 0:
            # either create new objects or call reset on each metric obj
            metrics_reg, metrics_cla, best_metric = get_metrics(args.task, args.threshold)

            # best_metric is the metric used to compare results across different evaluation runs
            # it's modified in place
            evaluate(rank=args.rank, gpu=args.gpu, task=args.task,
                     model=model, val_loader=val_loader,
                     metrics_reg=metrics_reg, metrics_cla=metrics_cla,
                     world_size=args.world_size, distributed=args.distributed,
                     best_metric=best_metric, pad=args.pad, transform=args.transform)

            if args.rank == 0:
                new_best = best_metric.better_than(current_best)
                if new_best:
                    current_best = best_metric
                    myprint("New best metric found - {}".format(current_best),
                            color='yellow', rank=args.rank)
                if new_best or epoch % args.save_freq == 0:
                    # give it the module attribute of the model (DistributedDataParallel wrapper)
                    if args.distributed:
                        save_model(
                            model.module, args.exp_dir, args.checkpoint_fname, epoch=epoch, is_best=new_best)
                    else:
                        save_model(
                            model, args.exp_dir, args.checkpoint_fname, epoch=epoch, is_best=new_best)


def infer_worker(gpu, ngpu_per_node, args, res_queue=None):

    args.rank = gpu if args.distributed else 0
    args.gpu = gpu

    torch.cuda.set_device(args.gpu)

    if args.distributed:
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)

    model = build_model(model = args.model, rank = args.rank, interval_size = args.interval_size,
                afunc = args.afunc, bn = args.bn, nblocks = args.nblocks, nblocks_cla = args.nblocks_cla,
                nfilt = args.nfilt, nfilt_cla = args.nfilt_cla, width = args.width, width_cla = args.width_cla,
                dil = args.dil, dil_cla = args.dil_cla, field = args.field, resume = args.resume,infer = args.infer,
                evaluate = args.eval, weights_path = args.weights_path, gpu = args.gpu, distributed = args.distributed)

    infer_dataset = DatasetInfer(args.infer_files)
    infer_sampler = None

    if args.distributed:
        infer_sampler = torch.utils.data.distributed.DistributedSampler(
            infer_dataset, shuffle=False)

    infer_loader = torch.utils.data.DataLoader(
        infer_dataset, batch_size=args.bs, shuffle=False,
        num_workers=args.num_workers, pin_memory=True, sampler=infer_sampler, drop_last=False
    )

    infer(rank=args.rank, gpu=args.gpu, task=args.task, model=model, infer_loader=infer_loader,
          print_freq=args.print_freq, res_queue=res_queue, pad=args.pad, transform=args.transform)


def eval_worker(gpu, ngpu_per_node, args, res_queue=None):

    args.rank = gpu if args.distributed else 0
    args.gpu = gpu

    torch.cuda.set_device(args.gpu)
    _logger.debug('Initialize Eval worker for Node %s' % (str(args.gpu)))

    if args.distributed:
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)
    model = build_model(model = args.model, rank = args.rank, interval_size = args.interval_size,
                afunc = args.afunc, bn = args.bn, nblocks = args.nblocks, nblocks_cla = args.nblocks_cla,
                nfilt = args.nfilt, nfilt_cla = args.nfilt_cla, width = args.width, width_cla = args.width_cla,
                dil = args.dil, dil_cla = args.dil_cla, field = args.field, resume = args.resume,infer = args.infer,
                evaluate = args.eval, weights_path = args.weights_path, gpu = args.gpu, distributed = args.distributed)

    eval_dataset = DatasetTrain(args.val_files)
    eval_sampler = None

    if args.distributed:
        eval_sampler = torch.utils.data.distributed.DistributedSampler(
            eval_dataset)

    eval_loader = torch.utils.data.DataLoader(
        eval_dataset, batch_size=args.bs, shuffle=False,
        num_workers=args.num_workers, pin_memory=True, sampler=eval_sampler, drop_last=False
    )

    metrics_reg, metrics_cla, best_metric = get_metrics(args.task, args.threshold)
    evaluate(rank=args.rank, gpu=args.gpu, task=args.task,
             model=model, val_loader=eval_loader, metrics_reg=metrics_reg, metrics_cla=metrics_cla,
             world_size=args.world_size, distributed=args.distributed,
             best_metric=best_metric, res_queue=res_queue, pad=args.pad, transform=args.transform)


def save_to_bedgraph(batch_range, item, channel, intervals, outfile, rounding=None, threshold=None):
    """
    Function to write out the tracks and peaks to bedGraphs. 
    Args:
        batch_range : List containing start and end position of batch to write.
        item : Output from the queue.
        channel : Channel to be written out.
        intervals : pandas object containing inference intervals.
        outfile : The output file to write the output to.
        rounding : If not None, round the scores to given value.
        threshold : if not None, threhsold the scores to given value.
    """
    keys, batch = item
    start = batch_range[0]
    end = batch_range[1]
    scores = batch[start:end,:,channel]
    # Round scores - for regression output
    if rounding is not None:
        scores = scores.astype('float64')
        # Sometimes np.around doesn't work with float32. To investigate.
        scores = np.around(scores, decimals=rounding)

    # Apply thresholding only to peaks
    if threshold is not None and channel == 1:
        scores = (scores > threshold).astype(int)
    # if the batch contains values > 0, write them
    if (scores > 0).any():
        # Select intervals corresponding to batch
        batch_intervals = intervals.iloc[keys.numpy()[start:end], :].copy()
        # Add scores to each interval
        batch_intervals['scores'] = np.split(scores, scores.shape[0])
        batch_intervals['scores'] = [x[0] for x in batch_intervals['scores']]
        # Select intervals with scores>0
        batch_intervals = batch_intervals.loc[scores.sum(axis=1)>0,:]
            
        # Expand each interval, combine with scores, and contract to smaller intervals
        batch_bg = intervals_to_bg(batch_intervals)
        df_to_bedGraph(batch_bg, outfile)


def writer(infer, intervals_file, exp_dir, result_fname,
           task, num_workers, infer_threshold, reg_rounding, cla_rounding,
           batches_per_worker, gen_bigwig, sizes_file, res_queue, prefix):
    """
    Function to write out the inference output to specified format. BedGraphs are generated by default. 
    If bigwig is requested, then bedGraph files are converted to bigwig.
    Args:
        args : Object holding all command line arguments
        res_queue : Queue containing the inference results
        prefix : The prefix to add to the output inference files. Useful when multiple files are
        inferenced together.
    """

    # We only pass one file at a time to the writer as a list.
    if not infer:
        assert False, "writer called but infer = False. Not sure what file to write?"

    intervals = pd.read_csv(intervals_file, sep='\t', header=None, names=['chrom', 'start', 'end'], 
         usecols=(0,1,2), dtype={'chrom':str, 'start':int, 'end':int})

    channels = []
    outputfiles = []
    out_base_path = os.path.join(exp_dir, prefix + "_" + result_fname)
    if task == "regression":
        channels = [0]
        outfiles = [os.path.join(out_base_path + ".track.bedGraph")]
        rounding = [reg_rounding]
    elif task == "classification":
        channels = [1]
        outfiles = [os.path.join(out_base_path + ".peaks.bedGraph")]
        rounding = [cla_rounding]
    elif task == "both":
        channels = [0, 1]
        outfiles = [os.path.join(out_base_path + ".track.bedGraph"),
                    os.path.join(out_base_path + ".peaks.bedGraph")]
        rounding = [reg_rounding, cla_rounding]

    # Temp dir used to save temp files during multiprocessing.
    temp_dir = tempfile.mkdtemp()
    for channel in channels:
        os.makedirs(os.path.join(temp_dir, str(channel)))

    batch_num = 0
    while 1:
        if not res_queue.empty():
            batch_num = batch_num + 1
            item = res_queue.get()
            if item == 'done':
                break
            keys, batch = item
            #No multi processing
            if num_workers == 0:
                start = 0
                end = batch.shape[0]
                for channel in channels:
                    with open(outfiles[channel], "a+") as outfile:
                        batch_bg = save_to_bedgraph([start, end], item, channel, intervals, outfile,
                                                    rounding=rounding[channel],
                                                    threshold=infer_threshold)
            else:
                num_jobs = math.ceil(batch.shape[0] / batches_per_worker)
                pool_size = 0

                if num_workers == -1: # spawn pool of processes
                    num_cpus = mp.cpu_count()
                    pool_size = min(num_jobs, num_cpus)
                else: # user specified # of processes
                    pool_size = num_workers

                pool = mp.Pool(pool_size)
                tmp_batch_ranges = [[i*batches_per_worker, (i+1)*batches_per_worker] for i in range(num_jobs)]
                # Force the last interval to capture remaining data.
                tmp_batch_ranges[-1][1] = batch.shape[0]
                all_intervals = [intervals]*len(tmp_batch_ranges)
                all_items = [item]*len(tmp_batch_ranges)
                for channel in channels:
                    temp_files = [os.path.join(temp_dir, str(channel), "{0:05}".format(num+batch_num)) for num in range(num_jobs)]
                    if infer_threshold is None:
                        map_args = list(zip(tmp_batch_ranges, all_items,
                                            [channel]*len(tmp_batch_ranges), all_intervals,
                                            temp_files, [rounding[channel]]*len(tmp_batch_ranges)))
                    else:
                        map_args = list(zip(tmp_batch_ranges, all_items,
                                            [channel]*len(tmp_batch_ranges), all_intervals,
                                            temp_files, [rounding[channel]]*len(tmp_batch_ranges),
                                            [args.infer_threshold]*len(tmp_batch_ranges)))

                    pool.starmap(save_to_bedgraph, map_args)

                    while True:
                        files = sorted(glob.glob(os.path.join(temp_dir, str(channel), "*")))
                        if len(files) == 1:
                            break
                        map_args = [(files[i*2], files[i*2+1]) for i in range(len(files)//2)]
                        res = pool.starmap(combiner, map_args)

                pool.close()
                pool.join()

    if num_workers != 0:
        for channel in channels:
            # Get last file in temp directory which has all the data
            files = glob.glob(os.path.join(temp_dir, str(channel), "*"))
            assert(len(files) == 1) # Only one file should be left after the combiner stage
            # move final files out of tmp folder
            shutil.move(files[0], outfiles[channel])

    # remove tmp folder
    shutil.rmtree(temp_dir)

    if (gen_bigwig):
        print ("Writing the output to bigwig files")
        for channel in channels:
            bedgraph_to_bigwig(outfiles[channel], sizes_file, prefix=None,deletebg=False, sort=True)


def combiner(f1, f2):
    _logger.debug("Combining {} and {}...".format(f1, f2))
    with open(f2, 'rb') as f:
        with open(f1, 'ab') as final:
            shutil.copyfileobj(f, final)
    os.remove(f2)


def main():
    args = parse_args()
    # Set log level
    if args.debug:
        _handler.setLevel(logging.DEBUG)
        _logger.setLevel(logging.DEBUG)

    _logger.debug(args)

    # Random seeds for reproducability.
    # set_random_seed(args.seed)

    # check gpu
    # TODO: add cpu support
    if not torch.cuda.is_available():
        raise Exception("No GPU available. Check your machine configuration.")

    # all output will be written in the exp_dir folder
    args.exp_dir = make_experiment_dir(
        args.label, args.out_home, timestamp=True)

    # train & resume
    ##########################################################################################
    if args.train or args.resume:
        args.train_files = gather_files_from_cmdline(args.train_files)
        args.val_files = gather_files_from_cmdline(args.val_files)
        _logger.debug("Training data:   " + "\n".join(args.train_files))
        _logger.debug("Validation data: " + "\n".join(args.val_files))

        # Get model parameters
        with h5py.File(args.train_files[0], 'r') as f:
            args.interval_size = f['data'].shape[1]
            args.batch_size = 1

        ngpus_per_node = torch.cuda.device_count()
        # WAR: gloo distributed doesn't work if world size is 1.
        # This is fixed in newer torch version - https://github.com/facebookincubator/gloo/issues/209
        args.distributed = False if ngpus_per_node == 1 else args.distributed

        _logger.info('Distributing to %s GPUS' % str(ngpus_per_node))

        if args.distributed:
            args.world_size = ngpus_per_node
            mp.spawn(train_worker, nprocs=ngpus_per_node,
                     args=(ngpus_per_node, args), join=True)
        else:
            assert_device_available(args.gpu)
            args.world_size = 1
            train_worker(args.gpu, ngpus_per_node, args, timers=Timers)

    # infer & eval
    ##########################################################################################
    if args.infer or args.eval:
        files = args.infer_files if args.infer else args.val_files
        files = gather_files_from_cmdline(files)
        for x in range(len(files)):
            infile = files[x]
            if args.infer:
                args.infer_files = [infile]
                _logger.debug("Inference data: ", args.infer_files)

                # Check that intervals, sizes and h5 file are all compatible.
                _logger.info('Checkng input files for compatibility')
                intervals = pd.read_csv(args.intervals_file, sep='\t', header=None, names=['chrom', 'start', 'end'],
                     usecols=(0,1,2), dtype={'chrom':str, 'start':int, 'end':int})
                sizes = pd.read_csv(args.sizes_file, sep='\t', header=None, names=['chrom', 'len'],
                     usecols=(0,1), dtype={'chrom':str, 'len':int}) 
                check_intervals(intervals, sizes, args.infer_files[0])

                # Delete intervals and sizes objects in main thread
                del intervals
                del sizes
            else:
                args.val_files = [infile]
                _logger.debug("Evaluation data: ", args.val_files)
            # Get model parameters
            with h5py.File(files[x], 'r') as f:
                args.interval_size = f['data'].shape[1]
                args.batch_size = 1

            prefix = os.path.basename(infile).split(".")[0]
            # setup queue and kick off writer process
            #############################################################
            manager = mp.Manager()
            res_queue = manager.Queue()
            # Create a keyword argument dictionary to pass into the multiprocessor
            keyword_args = {"infer" : args.infer, "intervals_file" : args.intervals_file, "exp_dir" : args.exp_dir,
                            "result_fname" : args.result_fname, "task" : args.task, "num_workers" : args.num_workers,
                            "infer_threshold" : args.infer_threshold, "reg_rounding" : args.reg_rounding,
                            "cla_rounding" : args.cla_rounding, "batches_per_worker" : args.batches_per_worker,
                            "gen_bigwig" : args.gen_bigwig, "sizes_file" : args.sizes_file,
                            "res_queue" : res_queue, "prefix" : prefix}
            write_proc = mp.Process(target=writer, kwargs=keyword_args)
            write_proc.start()
            #############################################################

            ngpus_per_node = torch.cuda.device_count()
            # WAR: gloo distributed doesn't work if world size is 1.
            # This is fixed in newer torch version - https://github.com/facebookincubator/gloo/issues/209
            args.distributed = False if ngpus_per_node == 1 else args.distributed

            worker = infer_worker if args.infer else eval_worker
            if args.distributed:
                args.world_size = ngpus_per_node
                mp.spawn(worker, nprocs=ngpus_per_node, args=(
                    ngpus_per_node, args, res_queue), join=True)
            else:
                assert_device_available(args.gpu)
                args.world_size = 1
                worker(args.gpu, ngpus_per_node, args, res_queue)

            # finish off writing
            #############################################################
            res_queue.put("done")
            _logger.info("Waiting for writer to finish...")
            write_proc.join()
            #############################################################


if __name__ == '__main__':
    main()
