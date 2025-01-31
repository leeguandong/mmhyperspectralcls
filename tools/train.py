import argparse
import os
import os.path as osp
import time

import torch
import numpy as np
import mmcv
from mmcv import Config, DictAction
from mmcv.runner import get_dist_info, init_dist
from mmhyperspectral import __version__
from mmhyperspectral.apis import set_random_seed, train_model, test_model
from mmhyperspectral.datasets import build_dataset
from mmhyperspectral.models import build_classifier
from mmhyperspectral.utils import collect_env, get_root_logger


def parse_args():
    parser = argparse.ArgumentParser(description='Train a model')
    parser.add_argument(
        '--config', default="../configs/resnet/resnet50_in.py",
        help='train config file path')
    parser.add_argument(
        '--work-dir', default="../results",
        help='the dir to save logs and models')
    parser.add_argument(
        '--resume-from', help='the checkpoint file to resume from')
    parser.add_argument(
        '--no-validate', default=False,
        help='whether not to evaluate the checkpoint during training')
    group_gpus = parser.add_mutually_exclusive_group()
    group_gpus.add_argument(
        '--device', default="cpu",
        help='device used for training')
    group_gpus.add_argument(
        '--gpus', type=int,
        help='number of gpus to use (only applicable to non-distributed training)')
    group_gpus.add_argument(
        '--gpu-ids', type=int, nargs='+',
        help='ids of gpus to use  (only applicable to non-distributed training)')
    parser.add_argument(
        '--seed', type=int, default=2021,
        help='random seed')
    parser.add_argument(
        '--deterministic', action='store_true',
        help='whether to set deterministic options for CUDNN backend.')
    parser.add_argument(
        '--options', nargs='+', action=DictAction,
        help='arguments in dict')
    parser.add_argument(
        '--launcher', choices=['none', 'pytorch', 'slurm', 'mpi'],
        default='none',
        help='job launcher')
    parser.add_argument(
        '--local_rank', type=int, default=0)
    parser.add_argument(
        '--autoscale-lr', action='store_true',
        help='automatically scale lr with the number of gpus')
    args = parser.parse_args()
    if 'LOCAL_RANK' not in os.environ:
        os.environ['LOCAL_RANK'] = str(args.local_rank)
    return args


def main():
    args = parse_args()

    cfg = Config.fromfile(args.config)
    if args.options is not None:
        cfg.merge_from_dict(args.options)
    # set cudnn_benchmark
    if cfg.get('cudnn_benchmark', False):
        torch.backends.cudnn.benchmark = True

    # work_dir is determined in this priority: CLI > segment in file > filename
    if args.work_dir is not None:
        # update configs according to CLI args if args.work_dir is not None
        cfg.work_dir = args.work_dir
    elif cfg.get('work_dir', None) is None:
        # use config filename as default work_dir if cfg.work_dir is None
        cfg.work_dir = osp.join('./work_dirs', osp.splitext(osp.basename(args.config))[0])
    if args.resume_from is not None:
        cfg.resume_from = args.resume_from
    if args.gpu_ids is not None:
        cfg.gpu_ids = args.gpu_ids
    else:
        cfg.gpu_ids = range(1) if args.gpus is None else range(args.gpus)

    # init distributed env first, since logger depends on the dist info.
    if args.launcher == 'none':
        distributed = False
    else:
        distributed = True
        init_dist(args.launcher, **cfg.dist_params)
        _, world_size = get_dist_info()
        cfg.gpu_ids = range(world_size)

    # create work_dir
    mmcv.mkdir_or_exist(osp.abspath(cfg.work_dir))
    # dump config
    cfg.dump(osp.join(cfg.work_dir, osp.basename(args.config)))
    # init the logger before other steps
    timestamp = time.strftime('%Y%m%d_%H%M%S', time.localtime())
    log_file = osp.join(cfg.work_dir, f'{timestamp}.log')
    logger = get_root_logger(log_file=log_file, log_level=cfg.log_level)

    # init the meta dict to record some important information such as
    # environment info and seed, which will be logged
    meta = dict()
    # log env info
    env_info_dict = collect_env()
    env_info = '\n'.join([(f'{k}: {v}') for k, v in env_info_dict.items()])
    dash_line = '-' * 60 + '\n'
    logger.info('Environment info:\n' + dash_line + env_info + '\n' + dash_line)
    meta['env_info'] = env_info

    # log some basic info
    logger.info(f'Distributed training: {distributed}')
    logger.info(f'Config:\n{cfg.pretty_text}')

    KAPPA, OA, AA, ELEMENT_ACC = [], [], [], []
    seed = [seed_value for seed_value in range(args.seed, args.seed + cfg.iter)]
    for index_iter in range(cfg.iter):
        # set random seeds
        if seed[index_iter] is not None:
            logger.info(f'Set random seed to {seed[index_iter]}, '
                        f'deterministic: {args.deterministic}')
            set_random_seed(seed[index_iter], deterministic=args.deterministic)
        cfg.seed = seed[index_iter]
        meta['seed'] = seed[index_iter]

        model = build_classifier(cfg.model)
        model.init_weights()
        base_dataset = build_dataset(cfg.data.train)
        datasets = [base_dataset.train_dataset]
        if len(cfg.workflow) == 2:
            datasets.append(base_dataset.val_dataset)
        if cfg.checkpoint_config is not None:
            cfg.checkpoint_config.meta = dict(
                mmhyperspectral_version=__version__,
                config=cfg.pretty_text,
                CLASSES=datasets[0].CLASSES)

        # add an attribute for visualization convenience
        train_model(
            model,
            datasets,
            cfg,
            distributed=distributed,
            validate=(not args.no_validate),
            timestamp=timestamp,
            device='cpu' if args.device == 'cpu' else 'cuda',
            meta=meta)

        # 加载模型来处理，把test的方法加载到这里来处理
        test_dataset = base_dataset.test_dataset
        test_indexes = test_dataset.test_indexes
        total_indexes = base_dataset.dataset.total_indexes
        gt = base_dataset.dataset.gt

        overall_acc, average_acc, kappa, each_acc = \
            test_model(
                model,
                test_dataset,
                test_indexes,
                total_indexes,
                gt,
                cfg,
                device='cpu' if args.device == 'cpu' else 'cuda')
        KAPPA.append(kappa)
        OA.append(overall_acc)
        AA.append(average_acc)
        ELEMENT_ACC.append(each_acc)

    logger.info(f'OAs for each iteration are:{OA}')
    logger.info(f'AAs for each iteration are:{AA}')
    logger.info(f'KAPPAs for each iteration are:{KAPPA}')
    logger.info(f'mean_OA ± std_OA is: {str(np.mean(OA))}±{str(np.std(OA))}')
    logger.info(f'mean_AA ± std_AA is:{str(np.mean(AA))}±{str(np.std(OA))} ')
    logger.info(f'mean_KAPPA ± std_KAPPA is:{str(np.mean(KAPPA))}±{str(np.std(KAPPA))}')
    logger.info(f'Mean of all elements in confusion matrix:{str(np.mean(ELEMENT_ACC,axis=0))}')
    logger.info(f'"Standard deviation of all elements in confusion matrix: {str(np.std(ELEMENT_ACC,axis=0))}"')


if __name__ == '__main__':
    main()
