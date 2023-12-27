"""
Run conseqcutive runner instances with the same parameters but on different reference files.
"""

import os

from utils import get_files_by_extension, get_config
from runner import runner, create_runner_parser
from logger import pipeline_logger

def adjust_args(args, ref_file, output_parent):
    args['reference_file'] = ref_file
    ref_name = ref_file.split('/')[-1].split('.fasta')[0]
    args['output_dir'] = output_parent + f'/{ref_name}'
    os.makedirs(args['output_dir'], exist_ok=True) 
    return args

def multi_ref_runner(args):
    log = pipeline_logger(logger_name='AccuNGS-Runner', log_folder=args['output_dir'])
    # prepare args
    ref_files = get_files_by_extension(args['references_dir'], 'fasta')
    del args['references_dir']
    output_parent = args['output_dir']
    # first run includes preparing the data
    if not os.path.exists(args['data_dir']):
        args['apply_prepare_data'] = True 
        first_ref = ref_files[0]
        args = adjust_args(args, first_ref, output_parent)
        runner(**args)
        # next runs dont need to prepare the data
        ref_files = ref_files[1:]
    else:
        log.info('data_dir exists, so trying to run with existing data.')
    args['apply_prepare_data'] = False 
    for ref_file in ref_files:
        args = adjust_args(args, ref_file, output_parent)
        runner(**args)


if __name__ == "__main__":
    parser = create_runner_parser()
    parser.add_argument("-rd", "--references_dir", help="path to directory containing fasta files to be used as references")
    parser.add_argument("-dd", "--data_dir", help="path to directory that will store the prepared data files")
    parser_args = vars(parser.parse_args())
    args = dict(get_config()['runner_defaults'])
    args.update({key: value for key, value in parser_args.items() if value is not None})
    multi_ref_runner(args)
