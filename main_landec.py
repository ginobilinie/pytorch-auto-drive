import time
import torch
if torch.backends.cudnn.version() < 8000:
    torch.backends.cudnn.benchmark = True
# torch.multiprocessing.set_sharing_strategy('file_system')
import resource
import argparse

from utils.args import parse_arg_cfg, read_config
from utils.runners import LaneDetTrainer, LaneDetTester


if __name__ == '__main__':
    # ulimit
    rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (8192, rlimit[1]))

    # Settings (user input > config > argparse defaults)
    parser = argparse.ArgumentParser(description='PyTorch Auto-drive')
    parser.add_argument('--exp-name', type=str,
                        help='Name of experiment')
    parser.add_argument('--workers', type=int,
                        help='Number of workers (threads) when loading data.'
                             'Recommend value for training: batch_size / 2')
    parser.add_argument('--batch-size', type=int,
                        help='input batch size. Recommend 4 times the training batch size in testing')
    parser.add_argument('--mixed-precision', action='store_true',
                        help='Enable mixed precision training')
    parser.add_argument('--continue-from', type=str,
                        help='Continue training from a previous checkpoint')
    parser.add_argument('--state', type=int,
                        help='Conduct validation(3)/final test(2)/fast validation(1)/normal training(0)')
    parser.add_argument('--world-size', type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist-url', type=str, help='url used to set up distributed training')
    parser.add_argument('--device', type=str, help='CPU is not recommended!')
    parser.add_argument('--config', type=str, help='Path to config file')
    parser.add_argument('--log-dir', type=str, help='Path prefix to save ckpt, etc.')

    defaults = {
        'exp_name': time.time(),
        'workers': 10,
        'batch_size': 8,
        'mixed_precision': False,
        'continue_from': None,
        'state': 0,
        'world_size': 0,
        'dist_url': 'env://',
        'device': 'cuda',
        'log_dir': ''
    }
    states = ['train', 'fastval', 'test', 'val']

    args = parser.parse_args()
    if args.config is None:
        raise ValueError('Must specify a config file!')

    # Parse configs and execute runner
    cfg = read_config(args.config)
    cfg_runner_key = 'train' if args.state == 0 else 'test'
    Runner = LaneDetTrainer if args.state == 0 else LaneDetTester
    args, cfg[cfg_runner_key] = parse_arg_cfg(args, cfg[cfg_runner_key], defaults)
    with open(args.exp_name + '_' + states[args.state] + '_cfg.txt', 'w') as f:
        f.write(str(cfg))
    runner = Runner(cfg=cfg, args=args)
    runner.run()
