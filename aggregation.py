"""
This script is used by runner.py in order to aggregate all the parallel computation output done by instances of
processing.py.

Input: directory containing output of basecalling from processing.py
Output: concatenations - concatenated outputs of processing.py
        read_id_prefix_file - a json file containing a dictionary of read prefixes to save memory in the other files.
        mutation_read_list - a file describing in which reads each mutation appeared.
        freqs - a frequencies file describing the different alleles and their frequencies for each position.
        consensus_with_indels - a fasta file of the majority frequency derived from the freqs file including indels
        consensus_without_indels - a fasta file of the majority frequency derived from the freqs file exclusing indels
        read_counter - a file counting how many alignments were called for each read.
"""
import argparse
import json
import os
import numpy as np
import pandas as pd

from utils import get_files_by_extension, concatenate_files_by_extension, create_consensus_file, get_files_in_dir, \
    get_sequence_from_fasta


def convert_called_bases_to_freqs(called_bases, reference):
    ref = pd.Series(list(reference), index=range(1, len(reference) + 1))
    dummy_bases = []
    for pos in ref.index:
        for base in ['A', 'G', 'T', 'C', '-']:
            dummy_bases.append({'ref_pos': pos, 'read_base': base, 'ref_base': ref[pos]})
    freq_dummies = pd.DataFrame.from_dict(dummy_bases)
    freqs = pd.concat([freq_dummies, called_bases])
    freqs = freqs.groupby(['ref_pos', 'read_base', 'ref_base']).agg({'read_id': 'nunique', 'overlap': 'sum',
                                                                     'quality': 'sum'})
    return freqs


def aggregate_called_bases(called_bases_files, reference):
    freqs = pd.DataFrame()
    for called_bases_file in called_bases_files:
        called_bases_df = pd.read_csv(called_bases_file, sep="\t")
        freqs_part = convert_called_bases_to_freqs(called_bases_df, reference)
        if freqs.empty:
            freqs = freqs_part
        else:
            freqs = freqs.add(freqs_part, fill_value=0)
    freqs = freqs.reset_index().rename(columns={'read_id': 'base_count'})
    if freqs.empty:
        return freqs
    freqs['overlap_ratio'] = (freqs['overlap'] / freqs['base_count']).fillna(0) / 2  # overlap counts twice!
    freqs['total_times_called'] = freqs['base_count'] * (1 + freqs['overlap_ratio'])
    freqs['avg_qscore'] = round((freqs['quality'] / freqs['total_times_called']).fillna(0), 1)
    freqs = freqs.drop(columns=['overlap', 'quality', 'total_times_called'])
    freqs['ref_pos'] = round(freqs['ref_pos'], 3)  # fix that floating point nonsense
    return freqs


def create_freqs_file(called_bases_files, output_path, reference):
    freqs = aggregate_called_bases(called_bases_files, reference)
    if not freqs.empty:
        coverage = freqs.groupby('ref_pos').base_count.sum()
        freqs['coverage'] = freqs.ref_pos.map(lambda pos: coverage[round(pos)])
        freqs['frequency'] = (freqs['base_count'] / freqs['coverage']).fillna(0)
        freqs['base_rank'] = freqs.read_base.nunique() - freqs.groupby('ref_pos').base_count.rank('min')
        freqs['probability'] = 1 - (1 - freqs["frequency"]) ** freqs['coverage']
        freqs = round(freqs, 4)
    freqs.to_csv(output_path, sep="\t", index=False)


def collect_reads_from_row(row):
    read_lists = {'other_reads': row.read_id, 'these_reads': row.read_id_this}
    for key, read_list in read_lists.items():
        if read_list is np.nan:
            read_lists[key] = ""
        else:
            read_lists[key] = read_list
    return set(read_lists['other_reads']) | set(read_lists['these_reads'])


def append_read_lists(read_list, this_read_list):
    read_list = read_list.join(this_read_list, rsuffix="_this", how='outer')
    read_list['read_id'] = read_list.apply(collect_reads_from_row, axis=1)
    return read_list[['read_id']]


def create_mutation_read_list_file(called_bases_files, output_path):
    read_list = pd.DataFrame()
    for bases_file in called_bases_files:
        this_read_list = pd.read_csv(bases_file, sep="\t").groupby(['ref_pos', 'read_base']).read_id.unique()
        if read_list.empty:
            read_list = pd.DataFrame(this_read_list)
        else:
            read_list = append_read_lists(read_list=read_list, this_read_list=this_read_list)
    read_list = read_list.reset_index()
    read_list['ref_pos'] = round(read_list['ref_pos'], 3)
    read_list.set_index(['ref_pos', 'read_base'], inplace=True)
    read_list.to_csv(output_path, sep='\t')


def aggregate_read_counters(read_counters, output_path):
    counter = {}
    for read_counter in read_counters:
        counter[read_counter] = pd.read_csv(read_counter, sep='\t')
    counters = pd.concat(counter.values())
    counters.groupby('read_id')['number_of_alignments'].sum().to_csv(output_path, sep='\t')


def update_prefix_dict(json_file, prefixes):
    if os.path.isfile(json_file):
        with open(json_file) as read_handle:
            read_id_prefix_dict = json.load(read_handle)
        next_prefix_value = 1
        if len(read_id_prefix_dict) > 0:
            next_prefix_value = max(read_id_prefix_dict.values()) + 1
    else:
        read_id_prefix_dict = {}
        next_prefix_value = 1
    for prefix in prefixes:
        if prefix not in read_id_prefix_dict.keys():
            read_id_prefix_dict[prefix] = next_prefix_value
            next_prefix_value += 1
    with open(json_file, 'w') as write_handle:
        json.dump(read_id_prefix_dict, write_handle)
    return read_id_prefix_dict


def trim_read_id_prefixes(files, read_id_prefix_file):
    prefix_length = 31
    for file in files:
        df = pd.read_table(file)
        if not df.empty:
            prefixes = df.read_id.str[:prefix_length].unique()
            prefix_dict = update_prefix_dict(read_id_prefix_file, prefixes)
    for file in files:
        df = pd.read_table(file)
        if not df.empty:
            df['read_id'] = df.read_id.map(lambda x: str(prefix_dict[x[:prefix_length]]) + "-" + x[prefix_length:])
            if file.endswith("bases"):
                df.to_csv(file, sep='\t', index=False, index_label='ref_pos')
            else:
                df.to_csv(file, sep='\t', index=False)


def aggregate_processed_output(input_dir, output_dir, min_coverage, min_frequency, cleanup):
    os.makedirs(output_dir, exist_ok=True)
    basecall_dir = os.path.join(input_dir, 'basecall')
    freqs_file_path = os.path.join(output_dir, "freqs.tsv")
    blast_dir = os.path.join(input_dir, 'blast')
    concatenate_files_by_extension(input_dir=blast_dir, extension="blast", remove_headers=False,
                                   output_path=os.path.join(output_dir, "blast.tsv"))
    if cleanup != 'Y':  # organise intermediary files
        called_bases_files = get_files_by_extension(basecall_dir, "called_bases")
        if len(called_bases_files) == 0:
            raise Exception(f"Could not find files of type *.called_bases in {input_dir}")
        basecall_files = get_files_in_dir(basecall_dir)
        read_id_prefix_file = os.path.join(output_dir, "read_id_prefixes.json")
        trim_read_id_prefixes(files=basecall_files, read_id_prefix_file=read_id_prefix_file)
        for file_type in ['called_bases', 'ignored_bases', 'suspicious_reads', 'ignored_reads']:
            concatenate_files_by_extension(input_dir=basecall_dir, extension=file_type,
                                           output_path=os.path.join(output_dir, f"{file_type}.tsv"))
    read_counters = get_files_by_extension(basecall_dir, "read_counter")
    aggregate_read_counters(read_counters=read_counters, output_path=os.path.join(output_dir, "read_counter.tsv"))
    create_consensus_file(freqs_file=freqs_file_path, min_coverage=min_coverage, min_frequency=min_frequency,
                          output_file=os.path.join(output_dir, "consensus_aligned_to_ref.fasta"), align_to_ref=True)
    create_consensus_file(freqs_file=freqs_file_path, min_coverage=min_coverage, min_frequency=min_frequency,
                          output_file=os.path.join(output_dir, "consensus.fasta"), align_to_ref=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input_dir", required=True,
                        help="Path to directory containing basecall files")
    parser.add_argument("-o", "--output_dir", required=True)
    parser.add_argument("-r", "--reference_file", required=True)
    parser.add_argument("-mc", "--min_coverage",
                        help="bases with less than this coverage will be substituted by Ns in the consensus")
    parser.add_argument("-mf", "--min_frequency",
                        help="bases with less than this frequency will be substituted by Ns in the consensus")

    args = parser.parse_args()
    aggregate_processed_output(input_dir=args.input_dir, output_dir=args.output_dir,
                               min_coverage=args.min_coverage, min_frequency=args.min_frequency)
