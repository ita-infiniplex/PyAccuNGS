"""
TODO: copy readme?
      remove haplotypes stuff
      remove blast mode
      remove db stuff?

This is where the magic happens!

It is meant to be able to run locally and in pbs_runner.py there is also specific support for pbs cluster systems.

___Overview___
The pipeline is divided into 4 parts each having it's own .py file.
I   -  Preperation (data_preperation.py)
II  -  Processing (processing.py)
III -  Aggregation (aggregation.py)
IV  -  Visual Summary (summarize.py)

I - Data Prepeation
Input: directory containing fastq/gz files or a directory containing such directories.
Output: fastq files in sizes ready for efficient processing.

Depending on currently availble RAM and CPUs or given values in -mm / --max_memory and -cc / --cpu_count the script
will divide the files into an efficient number of files to later run the processing script on.
If given an merge_opposing it will also merge the forward and backward reads in corresponding fastq files.

II - Processing
Input: fastq files from part I and a reference fasta file.
Output: called_bases - every base called and its attributes.
        ignored_bases - every base ignored, its attributes and why it was ignored.
        suspicious_reads - every suspicious read and why it is suspicious.
        ignored_reads - every read ignored and why.
        read_counter - every read and a number describing how many times it was aligned by blast.
        blast files - alignment output files.

This is where the main logic of the pipeline happens and the runner runs this on each of the fastq files in parallel.
Basically it first aligns the reads to the reference with blast and then goes over every read and nucleotide and decides
whether to filter them out or leave them in.

III - Aggregation
Input: directory containing output of basecalling of stage II
Output: concatenations - concatenated outputs of stage II
        read_id_prefix_file - a json file containing a dictionary of read prefixes to save memory in the other files.
        mutation_read_list - a file describing in which reads each mutation appeared.
        freqs - a frequencies file describing the different alleles and their frequencies for each position.
        consensus_aligned_to_ref - a fasta file of the majority frequency derived from the freqs file aligned to the original reference
        consensus - a fasta file of the majority frequency derived from the freqs file
        read_counter - a file counting how many alignments were called for each read.

IV - Haplotype Inference
Input: directory containing output of stage III
Output: linked_mutations - file containing pairs of mutatations, their pvalues and their frequencies.
        stretches - file containing the aggregations of the stretches by their frequencies.
"""
import argparse
import getpass
import json
import os
import shutil
import concurrent.futures
import Bio
import multiprocessing as mp
import pandas as pd
from Bio import pairwise2, SeqIO
from datetime import datetime
from data_preparation import prepare_data
from summarize import graph_summary, create_stats_file
from processing import process_fastq
from aggregation import aggregate_processed_output, create_freqs_file, create_mutation_read_list_file
from logger import pipeline_logger
from utils import get_files_in_dir, get_sequence_from_fasta, get_mp_results_and_report, create_consensus_file, \
    get_files_by_extension, concatenate_files_by_extension, get_config, md5_dir, md5_file


def parallel_process(processing_dir, fastq_files, reference_file, quality_threshold, task, evalue, dust, num_alignments,
                     soft_masking, perc_identity, mode, reads_overlap):
    with concurrent.futures.ProcessPoolExecutor() as executor:
        future_tasks = {executor.submit(process_fastq, fastq_file, reference=reference_file, output_dir=processing_dir,
                                        quality_threshold=quality_threshold, task=task, evalue=evalue, dust=dust,
                                        num_alignments=num_alignments, soft_masking=soft_masking,
                                        perc_identity=perc_identity, mode=mode, reads_overlap=reads_overlap): fastq_file
                        for fastq_file in fastq_files}

        for future in concurrent.futures.as_completed(future_tasks):
            future.result()


def set_filenames(output_dir, data_dir):
    filenames = {"freqs_file_path": os.path.join(output_dir, 'freqs.tsv'),
                 "linked_mutations_path": os.path.join(output_dir, 'linked_mutations.tsv'),
                 "mutation_read_list_path": os.path.join(output_dir, 'mutation_read_list.tsv'),
                 "stretches": os.path.join(output_dir, 'stretches.tsv'),
                 "blast_file": os.path.join(output_dir, 'blast.tsv'),
                 "read_counter_file": os.path.join(output_dir, 'read_counter.tsv'),
                 "summary_graphs": os.path.join(output_dir, 'summary.png'),
                 "processing_dir": os.path.join(output_dir, "processing"),
                 'linked_mutations_dir': os.path.join(output_dir, "linked_mutations")}
    if data_dir is None:
        filenames['data_dir'] = os.path.join(output_dir, "data")
    else:
        filenames['data_dir'] = data_dir
    filenames['basecall_dir'] = os.path.join(filenames['processing_dir'], 'basecall')
    os.makedirs(filenames['data_dir'], exist_ok=True)
    os.makedirs(filenames["basecall_dir"], exist_ok=True)
    return filenames


def create_consensus_and_check_alignment_with_ref(reference_file, align_to_ref, min_coverage, iteration_data_dir, basecall_dir,
                                                  iteration_counter, min_frequency):
    reference = get_sequence_from_fasta(reference_file)
    freqs_file_path = os.path.join(iteration_data_dir, f"freqs_{iteration_counter}.tsv")
    called_bases_files = get_files_by_extension(basecall_dir, "called_bases")
    if len(called_bases_files)==0:
        return 0, None
    create_freqs_file(called_bases_files=called_bases_files, output_path=freqs_file_path, reference=reference)
    if align_to_ref == "Y":
        consensus_path = os.path.join(iteration_data_dir, f"consensus_aligned_to_ref_{iteration_counter}.fasta")
        align_to_ref = True
    else:
        consensus_path = os.path.join(iteration_data_dir, f"consensus_{iteration_counter}.fasta")
        align_to_ref = False
    create_consensus_file(freqs_file=freqs_file_path, min_frequency=min_frequency,
                          min_coverage=min_coverage, output_file=consensus_path, align_to_ref=align_to_ref)
    consensus = get_sequence_from_fasta(consensus_path)
    #TODO: get helpdesk to create the right environment and remove this crap..
    alignment = pairwise2.align.globalxx(consensus, reference)[0]
    if float(Bio.__version__) > 1.76:
        alignment_score = alignment.score / max(len(consensus), len(reference))
        alignment = alignment.seqA
    else:
        alignment_score = int(alignment[2]) / max(len(consensus), len(reference))
        alignment = alignment[0]
    return alignment_score, alignment


def get_consensus_path(basecall_iteration_counter, align_to_ref, iteration_data_dir):
    consensus = os.path.join(iteration_data_dir, "consensus")
    if align_to_ref == "Y":
        consensus += "_aligned_to_ref"
    consensus += f"_{basecall_iteration_counter}.fasta"
    return consensus


def update_meta_data(output_dir, status, db_path, params=None):
    # TODO: write running time in meta_data
    json_file = os.path.join(output_dir, 'meta_data.json')
    if params is not None:
        meta_data = params
        del meta_data['filenames']
        del meta_data['log']
        meta_data['username'] = getpass.getuser()
        meta_data['start_time'] = datetime.now().strftime('%Y-%m-%d-%H:%M')
    else:
        with open(json_file) as read_handle:
            meta_data = json.load(read_handle)
    meta_data['status'] = status
    with open(json_file, 'w') as write_handle:
        json.dump(meta_data, write_handle)
    build_db(db_path)


def build_db(db_path):
    # TODO: get user feedback on what db functionality should be
    os.makedirs(db_path, exist_ok=True)
    outputs = [f.path for f in os.scandir(db_path) if f.is_dir()]
    db_rows = []
    for directory in outputs:
        json_file = os.path.join(directory, "meta_data.json")
        if os.path.isfile(json_file):
            with open(json_file) as read_handle:
                meta_data = json.load(read_handle)
            db_rows.append(meta_data)
    db = pd.DataFrame.from_dict(db_rows)
    db.to_csv(os.path.join(db_path, 'db.tsv'), sep='\t')


def create_dir_also_if_exists(output_dir):
    try:
        os.makedirs(output_dir, exist_ok=False)
    except FileExistsError:
        i = 2
        while i < 1000:
            try:
                output_dir = f"{output_dir}_{i}"
                os.makedirs(output_dir, exist_ok=False)
            except FileExistsError:
                i += 1
    return output_dir


def assign_output_dir(db_path, alias=None):
    now = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    output_dir_name = now
    if alias:
        output_dir_name = alias + "_" + output_dir_name
    output_dir = os.path.join(db_path, output_dir_name)
    output_dir = create_dir_also_if_exists(output_dir)
    return output_dir


def process_data(align_to_ref, dust, evalue, fastq_files, log, max_basecall_iterations, min_frequency,
                 min_coverage, mode, num_alignments, overlapping_reads, output_dir, perc_identity, processing_dir,
                 quality_threshold, reference_file, soft_masking, task, basecall_dir):
    alignments = []
    for basecall_iteration_counter in range(1, max_basecall_iterations + 1):
        log.info(f"Processing fastq files iteration {basecall_iteration_counter}/{max_basecall_iterations}")
        parallel_process(processing_dir=processing_dir, fastq_files=fastq_files, reference_file=reference_file,
                         quality_threshold=quality_threshold, task=task, evalue=evalue, dust=dust, mode=mode,
                         num_alignments=num_alignments, soft_masking=soft_masking, perc_identity=perc_identity,
                         reads_overlap=overlapping_reads)
        iteration_data_dir = os.path.join(output_dir, 'iteration_data')
        os.makedirs(iteration_data_dir, exist_ok=True)
        alignment_score, alignment = create_consensus_and_check_alignment_with_ref(reference_file=reference_file,
                                                                                   iteration_counter=basecall_iteration_counter,
                                                                                   basecall_dir=basecall_dir,
                                                                                   align_to_ref=align_to_ref,
                                                                                   iteration_data_dir=iteration_data_dir,
                                                                                   min_coverage=min_coverage,
                                                                                   min_frequency=min_frequency)
        
        alignments.append(alignment)
        log.info(f'Iteration alignment score: {round(alignment_score, 4)}')
        if alignment_score == 1:
            break
        consensus_path = get_consensus_path(basecall_iteration_counter=basecall_iteration_counter,
                                            align_to_ref=align_to_ref, iteration_data_dir=iteration_data_dir)
        reference_file = consensus_path
    return reference_file, alignments, basecall_iteration_counter


def validate_input(output_dir, input_dir, reference_file, mode):
    if mode != 'RefToSeq' and mode != 'SeqToRef':
        raise Exception("blast mode must be either RefToSeq or SeqToRef! ")
    if os.path.exists(output_dir):
        list_dir = os.listdir(output_dir)
        if len(list_dir) > 0:
            if not ((len(list_dir) == 1) and (list_dir[0] == 'pbs_logs')):
                raise Exception("output_dir must be path to a new or empty directory!")
    if not os.path.isfile(reference_file):
        raise Exception("reference_file must exist!")
    else:
        with open(reference_file, "r") as handle:
            fasta = SeqIO.parse(handle, "fasta")
            if not any(fasta):
                raise Exception("reference_file must be of type fasta!")
    if not os.path.isdir(input_dir):
        raise Exception("Input_dir must exist!")
    #files_fasta = get_files_by_extension(input_dir, "fastq") + get_files_by_extension(input_dir, "fq")
    #files_fastagz = get_files_by_extension(input_dir, "fastq.gz") + get_files_by_extension(input_dir, "fq.gz")
    #if len(files_fasta) == 0 and len(files_fastagz) == 0:
    #    raise Exception("Could not find files ending with '.fastq', '.fq', 'fastq.gz' or 'fq,gz' in input_dir !")


def try_to_rmtree(path_to_delete, retry_attempts=5):
    # this is a hack to avoid a bug rmtree throws about a non-empty dir sometimes...
    if retry_attempts:
        try:
            if os.path.isdir(path_to_delete):
                shutil.rmtree(path_to_delete)
        except:
            try_to_rmtree(path_to_delete, retry_attempts-1)


def remove_unnecessary_files(dirs_to_remove, files_to_remove):
    for file_path in files_to_remove:
        os.remove(file_path)
    for dir_path in dirs_to_remove:
        try_to_rmtree(dir_path)


def runner(input_dir, reference_file, output_dir, max_basecall_iterations, min_coverage, db_comment,
           quality_threshold, blast_task, blast_evalue, blast_dust, blast_num_alignments, blast_soft_masking, 
           blast_perc_identity, blast_mode, align_to_ref, cleanup, min_frequency, cpu_count, overlapping_reads, 
           db_path, max_memory, apply_prepare_data=True, data_dir=None):
    if not db_path:
        db_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'db')
    if not output_dir:
        output_dir = assign_output_dir(db_path)
    validate_input(output_dir, input_dir, reference_file, blast_mode)
    log = pipeline_logger(logger_name='AccuNGS-Runner', log_folder=output_dir)
    try:
        filenames = set_filenames(output_dir=output_dir, data_dir=data_dir)
        if not cpu_count:
            cpu_count = mp.cpu_count()
        #TODO: do something with these..
        #input_dir_hash = md5_dir(input_dir)
        #reference_file_hash = md5_file(reference_file)
        params = locals().copy()
        update_meta_data(params=params, output_dir=output_dir, status='Setting up...', db_path=db_path)
        log.debug(f"runner params: {params}")  # TODO: why does this contain status..?
        log.info("Preparing data")
        update_meta_data(output_dir=output_dir, status='Preparing data...', db_path=db_path)
        if apply_prepare_data:
            prepare_data(input_dir=input_dir, output_dir=filenames['data_dir'], overlapping_reads=overlapping_reads,
                        cpu_count=cpu_count, max_memory=max_memory)
        data_files = get_files_in_dir(filenames['data_dir'])
        fastq_files = [file_path for file_path in data_files if ".part_" in os.path.basename(file_path)]
        log.info(f"Processing {len(fastq_files)} fastq files.")
        update_meta_data(output_dir=output_dir, status='Processing data...', db_path=db_path)
        reference_file, alignments, iterations = process_data(
            align_to_ref=align_to_ref, dust=blast_dust, min_frequency=min_frequency,
            evalue=blast_evalue, fastq_files=fastq_files, log=log, soft_masking=blast_soft_masking,
            max_basecall_iterations=max_basecall_iterations, min_coverage=min_coverage,
            mode=blast_mode, num_alignments=blast_num_alignments, overlapping_reads=overlapping_reads,
            output_dir=output_dir, perc_identity=blast_perc_identity, reference_file=reference_file,
            processing_dir=filenames['processing_dir'], quality_threshold=quality_threshold,
            task=blast_task, basecall_dir=filenames['basecall_dir'])
        log.info("Aggregating processed fastq files outputs...")
        last_freqs = os.path.join(output_dir, 'iteration_data', f'freqs_{iterations}.tsv')
        if not os.path.exists(last_freqs):
            log.info('Could not find matches for the reference in the data!')
        else:
            shutil.copy(last_freqs, filenames['freqs_file_path'])
            aggregate_processed_output(input_dir=filenames['processing_dir'], output_dir=output_dir,
                                    min_coverage=min_coverage, min_frequency=min_frequency, cleanup=cleanup)
            create_stats_file(output_dir, filenames, alignments)
            log.info("Generating graphs...")
            graph_summary(freqs_file=filenames['freqs_file_path'], blast_file=filenames['blast_file'],
                        read_counter_file=filenames['read_counter_file'],
                        output_file=filenames['summary_graphs'], min_coverage=min_coverage)  # TODO: drop low quality mutations?
            log.info(f"Most outputs are ready in {output_dir} !")
            if cleanup == "Y":
                dirs_to_remove = [filenames['basecall_dir']]
                if data_dir is None:
                    dirs_to_remove.append(filenames['data_dir'])
                files_to_remove = [filenames['blast_file']]
                log.info(f"Removing intermediary files to save space...")
                remove_unnecessary_files(dirs_to_remove, files_to_remove)
        update_meta_data(output_dir=output_dir, status='Done', db_path=db_path)
        log.info(f"Done!")
    except Exception as e:
        log.exception(e)
        update_meta_data(output_dir=output_dir, status="Failed! see logs for details.", db_path=db_path)


def create_runner_parser():
    # TODO: dynamic defaults?
    parser = argparse.ArgumentParser(description="Note: Default parameter values are retrieved from config.ini in your "
                                                 "installation directory.")
    parser.add_argument("-i", "--input_dir", required=True,
                        help="Path to directory containing fastq/gz files or sub directories containg fastq/gz files.")
    parser.add_argument("-o", "--output_dir", help="A directory for output files. "
                                                   "If none is given will put it in the db")
    parser.add_argument("-r", "--reference_file", required=False, help="Full path to reference file (including "
                                                                      "extension) of type fasta to align against.")
    parser.add_argument("-m", "--max_basecall_iterations", type=int,
                        help="Number of times to rerun with previous consensus as the new reference before giving up.")
    parser.add_argument("-or", "--overlapping_reads",
                        help="Y/N/P, run pipeline with, without, or with partial overlapping reads. Y- merge opposing "
                             "reads in the same directory and drop non overlapping areas of the reads. "
                             "P - Merge opposing reads but keep non overlapping areas."
                             "N - No merge, assume reads are independent. "
                             "Y & P assume 2 fastq/gz files in each sub directory of the input_dir.")
    parser.add_argument("-bt", "--blast_task", help="blast's task parameter")
    parser.add_argument("-be", "--blast_evalue", help="blast's e value parameter", type=float)
    parser.add_argument("-bd", "--blast_dust", help="blast's dust parameter")
    parser.add_argument("-bn", "--blast_num_alignments", type=int, help="blast's num_alignments parameter")
    parser.add_argument("-bp", "--blast_perc_identity", type=float, help="blast's perc_identity parameter")
    parser.add_argument("-bs", "--blast_soft_masking", help="blast's soft_masking parameter")
    parser.add_argument("-bm", "--blast_mode", help="RefToSeq or SeqToRef")  # TODO: docs
    parser.add_argument("-qt", "--quality_threshold", type=int,
                        help="phred score must be higher than this to be included")
    parser.add_argument("-mc", "--min_coverage", type=int,
                        help="positions with less than this coverage will be substituted by Ns in the consensus")
    parser.add_argument("-mf", "--min_frequency", type=float,
                        help="positions with less than this frequency will be substituted by Ns in the consensus")
    parser.add_argument("-ar", "--align_to_ref", help="Y/N, generate consensus aligned to the original reference")
    parser.add_argument("-c", "--cleanup", help="Remove intermediary files when done in order to save space")
    parser.add_argument("-cc", "--cpu_count", help="max number of cpus to use (None means all)", type=int)
    parser.add_argument("-db", "--db_path", help='path to db directory')
    parser.add_argument("-dbc", "--db_comment", help='comment to store in db')
    parser.add_argument("-mm", "--max_memory", help='limit memory usage to this many megabytes '
                                                    '(None would use available memory when starting to run)')
    return parser


if __name__ == "__main__":
    parser = create_runner_parser()
    parser_args = vars(parser.parse_args())
    args = dict(get_config()['runner_defaults'])
    args.update({key: value for key, value in parser_args.items() if value is not None})
    runner(input_dir=args['input_dir'], output_dir=args['output_dir'], reference_file=args['reference_file'],
           max_basecall_iterations=int(args['max_basecall_iterations']), overlapping_reads=args['overlapping_reads'],
           quality_threshold=int(args['quality_threshold']), blast_task=args['blast_task'], max_memory=args['max_memory'],
           blast_evalue=float(args['blast_evalue']), blast_dust=args['blast_dust'],
           blast_num_alignments=int(args['blast_num_alignments']),
           blast_mode=args['blast_mode'], blast_perc_identity=float(args['blast_perc_identity']), cpu_count=args['cpu_count'],
           min_coverage=int(args['min_coverage']), db_comment=args['db_comment'],
           blast_soft_masking=args['blast_soft_masking'], min_frequency=float(args['min_frequency']),
           cleanup=args['cleanup'], align_to_ref=args['align_to_ref'], db_path=args['db_path'])
