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

r"""Creates non-overlapping intervals tiling across the whole genome or given chromosomes.

Workflow:
    1. Reads chromosome names and sizes for the genome
    2. Produces intervals tiling across the genome or given chromosomes
    3. Optionally splits intervals into train, val, holdout chromosomes
    4. Optionally down-samples intervals without peaks in a given dataset

Output:
    BED file containg intervals spanning all provided chromosomes, OR
    BED files containing training, validation and holdout intervals.

Examples:
    Whole-genome intervals of size 50 kb:
        python get_intervals.py example/reference/hg19.chrom.sizes 50000 ./ --wg
    Train/val/holdout intervals of size 50 kb
        python get_intervals.py example/reference/hg19.auto.sizes 50000 ./ --val chr20 \
        --holdout chr10
    Train/val/holdout intervals of size 50 kb
    (upsampling peaks to 1/2 of the final training set)
        python get_intervals.py example/reference/hg19.auto.sizes 50000 ./ --val chr20 \
        --holdout chr10 \
        --peakfile HSC-1.merge.filtered.depth_1000000_peaks.bw \
        --nonpeak 1

"""

# Import requirements
import argparse

import logging

from claragenomics.io.bigwigio import check_bigwig_intervals_peak
from claragenomics.io.bedio import read_sizes

import pandas as pd


# Set up logging
log_formatter = logging.Formatter(
    '%(levelname)s:%(asctime)s:%(name)s] %(message)s')
_logger = logging.getLogger('AtacWorks-intervals')
_handler = logging.StreamHandler()
_handler.setLevel(logging.INFO)
_handler.setFormatter(log_formatter)
_logger.setLevel(logging.INFO)
_logger.addHandler(_handler)


def get_tiling_intervals(sizes, intervalsize, shift=None):
    """Produce intervals of given chromosomes.

    Tile from start to end of given chromosomes, shifting by given length.

    Args:
        sizes: Pandas df containing columns 'chrom' and 'length',
            with name and length of required chromosomes
        intervalsize: length of intervals
        shift: distance between starts of successive intervals.

    Returns:
        Pandas DataFrame containing chrom, start, and end of tiling intervals.

    """
    # Default: non-overlapping intervals
    if shift is None:
        shift = intervalsize

    # Create empty DataFrame
    intervals = pd.DataFrame()

    # Create intervals per chromosome
    for i in range(len(sizes)):
        chrom = sizes.iloc[i, 0]
        chrend = sizes.iloc[i, 1]
        starts = range(0, chrend - (intervalsize + 1), shift)
        ends = [x + intervalsize for x in starts]
        intervals = intervals.append(pd.DataFrame(
            {'chrom': chrom, 'start': starts, 'end': ends}))

    # Eliminate intervals that extend beyond chromosome size
    intervals = intervals.merge(sizes, on='chrom')
    intervals = intervals[intervals['end'] < intervals['size']]

    return intervals.loc[:, ('chrom', 'start', 'end')]


def parse_args():
    """Parse command line arguments.

    Return:
        parsed argument object.

    """
    parser = argparse.ArgumentParser(description='DenoiseNet interval script')
    parser.add_argument('sizes', type=str,
                        help='Path to chromosome sizes file')
    parser.add_argument('intervalsize', type=int, help='Interval size')
    parser.add_argument('prefix', type=str, help='Output file prefix')
    parser.add_argument(
        '--shift', type=int,
        help='Shift between training intervals.\
            If not given, intervals are non-overlapping')
    parser.add_argument('--wg', action='store_true',
                        help='Produce one set of intervals for whole genome')
    parser.add_argument('--val', type=str, help='Chromosome for validation')
    parser.add_argument('--holdout', type=str, help='Chromosome to hold out')
    parser.add_argument('--nonpeak', type=int,
                        help='Ratio between number of non-peak\
                        intervals and peak intervals', default=1)
    parser.add_argument('--peakfile', type=str,
                        help='Path to bigWig file containing peaks. \
                        Use when setting --nonpeak. Use peak2bw.py to create this bigWig file.')
    args = parser.parse_args()
    return args


def main():
    """Read chromosome sizes and generate intervals."""
    args = parse_args()

    # Read chromosome sizes
    sizes = read_sizes(args.sizes)

    # Generate intervals
    if args.wg:

        # Generate intervals tiling across all chromosomes in the sizes file
        _logger.info("Generating intervals tiling across all chromosomes in sizes file: " + args.sizes)
        intervals = get_tiling_intervals(sizes, args.intervalsize, args.shift)

        # Write to file
        intervals.to_csv(args.prefix + '.genome_intervals.bed',
                         sep='\t', index=False, header=False)

    else:

        # Generate training intervals - can overlap
        _logger.info("Generating training intervals")
        train_sizes = sizes[sizes['chrom'] != args.val]
        if args.holdout is not None:
            train_sizes = train_sizes[train_sizes['chrom'] != args.holdout]
        train = get_tiling_intervals(
            train_sizes, args.intervalsize, args.shift)

        # Optional - Set fraction of training intervals to contain peaks
        # TODO: up-sample these intervals in pytorch instead?
        if args.peakfile is not None:
            _logger.info('Finding intervals with peaks')
            train['peak'] = check_bigwig_intervals_peak(train, args.peakfile)
            _logger.info('{} of {} intervals contain peaks.'.format(
                train['peak'].sum(), len(train)))
            train_peaks = train[train['peak']].copy()
            train_nonpeaks = train[train['peak'] is False].sample(
                args.nonpeak * len(train_peaks))
            train = train_peaks.append(train_nonpeaks)
            train = train.iloc[:, :3]
            _logger.info('Generated {} peak and {} non-peak\
                     training intervals.'.format(
                len(train_peaks), len(train_nonpeaks)))

        # Write to file
        train.to_csv(args.prefix + '.training_intervals.bed',
                     sep='\t', index=False, header=False)

        # Generate validation intervals - do not overlap
        _logger.info("Generating val intervals")
        val_sizes = sizes[sizes['chrom'] == args.val]
        val = get_tiling_intervals(
            val_sizes, args.intervalsize)

        # Write to file
        val.to_csv(args.prefix + '.val_intervals.bed',
                   sep='\t', index=False, header=False)

        # Generate holdout intervals - do not overlap
        if args.holdout is not None:
            _logger.info("Generating holdout intervals")
            holdout_sizes = sizes[sizes['chrom'] == args.holdout]
            holdout = get_tiling_intervals(
                holdout_sizes, args.intervalsize)

            # Write to file
            holdout.to_csv(args.prefix + '.holdout_intervals.bed',
                           sep='\t', index=False, header=False)

    _logger.info('Done!')


if __name__ == "__main__":
    main()
