# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import argparse
import collections
import json
import os
import random
import sys
import time
import uuid

import numpy as np
import PIL
import torch
import torchvision
import torch.utils.data

from domainbed import datasets
from domainbed import hparams_registry
from domainbed import algorithms
from domainbed.lib import misc
from domainbed.lib.fast_data_loader import InfiniteDataLoader, FastDataLoader

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Domain generalization')
    parser.add_argument('--data_dir', type=str)
    # parser.add_argument('--dataset', type=str, default="RotatedMNIST")
    parser.add_argument('--dataset', type=str, default="ColoredMNIST_PLUS")
    parser.add_argument('--algorithm', type=str, default="ERM")
    parser.add_argument('--task', type=str, default="domain_generalization",
        choices=["domain_generalization", "domain_adaptation"])
    parser.add_argument('--hparams', type=str,
        help='JSON-serialized hparams dict')
    parser.add_argument('--hparams_seed', type=int, default=0,
        help='Seed for random hparams (0 means "default hparams")')
    parser.add_argument('--trial_seed', type=int, default=0,
        help='Trial number (used for seeding split_dataset and '
        'random_hparams).')
    parser.add_argument('--seed', type=int, default=0,
        help='Seed for everything else')
    parser.add_argument('--steps', type=int, default=None,
        help='Number of steps. Default is dataset-dependent.')
    parser.add_argument('--checkpoint_freq', type=int, default=None,
        help='Checkpoint every N steps. Default is dataset-dependent.')
    parser.add_argument('--test_envs', type=int, nargs='+', default=[2])
    parser.add_argument('--val_envs', type=int, nargs='*', default=[])   # setting this triggers a new model selection method
    parser.add_argument('--output_dir', type=str, default="train_output")
    parser.add_argument('--holdout_fraction', type=float, default=0.2)
    parser.add_argument('--uda_holdout_fraction', type=float, default=0,
        help="For domain adaptation, % of test to use unlabeled for training.")
    parser.add_argument('--skip_model_save', action='store_true')
    parser.add_argument('--save_model_every_checkpoint', action='store_true')
    args = parser.parse_args()

    # If we ever want to implement checkpointing, just persist these values
    # every once in a while, and then load them from disk here.
    start_step = 0
    algorithm_dict = None

    os.makedirs(args.output_dir, exist_ok=True)
    sys.stdout = misc.Tee(os.path.join(args.output_dir, 'out.txt'))
    sys.stderr = misc.Tee(os.path.join(args.output_dir, 'err.txt'))

    print("Environment:")
    print("\tPython: {}".format(sys.version.split(" ")[0]))
    print("\tPyTorch: {}".format(torch.__version__))
    print("\tTorchvision: {}".format(torchvision.__version__))
    print("\tCUDA: {}".format(torch.version.cuda))
    print("\tCUDNN: {}".format(torch.backends.cudnn.version()))
    print("\tNumPy: {}".format(np.__version__))
    print("\tPIL: {}".format(PIL.__version__))

    print('Args:')
    for k, v in sorted(vars(args).items()):
        print('\t{}: {}'.format(k, v))

    if args.hparams_seed == 0:
        hparams = hparams_registry.default_hparams(args.algorithm, args.dataset)
    else:
        hparams = hparams_registry.random_hparams(args.algorithm, args.dataset,
            misc.seed_hash(args.hparams_seed, args.trial_seed))
    if args.hparams:
        hparams.update(json.loads(args.hparams))

    print('HParams:')
    for k, v in sorted(hparams.items()):
        print('\t{}: {}'.format(k, v))

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    if torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    print(f'Device: {device}')
    if device == 'cuda':
        print(f'Visible device count: {torch.cuda.device_count()}')
        visible_devices = os.environ.get('CUDA_VISIBLE_DEVICES', 'all')
        print(f'Visible device id(s): {visible_devices}')

    if args.dataset in vars(datasets):
        dataset = vars(datasets)[args.dataset](args.data_dir,
            args.test_envs, hparams)
    else:
        raise NotImplementedError

    in_splits = []
    out_splits = []
    uda_splits = []

    if args.val_envs:  # use OoD validation model selection method
        test_in_splits = []

        if args.uda_holdout_fraction > 0:
            raise NotImplementedError

        for env_i, env in enumerate(dataset):
            if env_i in args.val_envs:
                out_weights = misc.make_weights_for_balanced_classes(env) if hparams['class_balanced'] else None
                in_splits.append((None, None))  # dummy
                out_splits.append((env, out_weights))
            elif env_i in args.test_envs:
                in_weights = misc.make_weights_for_balanced_classes(env) if hparams['class_balanced'] else None
                in_splits.append((env, in_weights))
                test_in_splits.append((env, in_weights))
            else:
                out, _ = misc.split_dataset(env,
                    int(len(env)*args.holdout_fraction),
                    misc.seed_hash(args.trial_seed, env_i))
                in_weights = misc.make_weights_for_balanced_classes(env) if hparams['class_balanced'] else None
                in_splits.append((env, in_weights))   # use all data to train
                out_weights = misc.make_weights_for_balanced_classes(out) if hparams['class_balanced'] else None
                out_splits.append((out, out_weights)) # a small sample used to check progress

    else:
        for env_i, env in enumerate(dataset):
            uda = []

            out, in_ = misc.split_dataset(env,
                int(len(env)*args.holdout_fraction),
                misc.seed_hash(args.trial_seed, env_i))

            if env_i in args.test_envs:
                uda, in_ = misc.split_dataset(in_,
                    int(len(in_)*args.uda_holdout_fraction),
                    misc.seed_hash(args.trial_seed, env_i))

            if hparams['class_balanced']:
                in_weights = misc.make_weights_for_balanced_classes(in_)
                out_weights = misc.make_weights_for_balanced_classes(out)
                if uda is not None:
                    uda_weights = misc.make_weights_for_balanced_classes(uda)
            else:
                in_weights, out_weights, uda_weights = None, None, None
            in_splits.append((in_, in_weights))
            out_splits.append((out, out_weights))
            if len(uda):
                uda_splits.append((uda, uda_weights))

    if args.task == "domain_adaptation" and len(uda_splits) == 0:
        raise ValueError("Not enough unlabeled samples for domain adaptation.")

    train_loaders = [InfiniteDataLoader(
        dataset=env,
        weights=env_weights,
        batch_size=hparams['batch_size'],
        num_workers=dataset.N_WORKERS)
        for i, (env, env_weights) in enumerate(in_splits)
        if i not in args.test_envs + args.val_envs]

    uda_loaders = [InfiniteDataLoader(
        dataset=env,
        weights=env_weights,
        batch_size=hparams['batch_size'],
        num_workers=dataset.N_WORKERS)
        for i, (env, env_weights) in enumerate(uda_splits)
        if i in args.test_envs]

    if args.val_envs:
        eval_loaders = [FastDataLoader(
            dataset=env,
#             batch_size=(4 if args.dataset.startswith('NICO') else 64),
            batch_size=64,
            num_workers=dataset.N_WORKERS)
            for env, _ in (test_in_splits + out_splits + uda_splits)]
        eval_weights = [None for _, weights in (test_in_splits + out_splits + uda_splits)]

        eval_loader_names = ['env{}_in'.format(i)
            for i in range(len(dataset)) if i in args.test_envs]
        eval_loader_names += ['env{}_out'.format(i)
            for i in range(len(dataset)) if i not in args.test_envs]
    else:
        eval_loaders = [FastDataLoader(
            dataset=env,
            batch_size=64,
            num_workers=dataset.N_WORKERS)
            for env, _ in (in_splits + out_splits + uda_splits)]
        eval_weights = [None for _, weights in (in_splits + out_splits + uda_splits)]

        eval_loader_names = ['env{}_in'.format(i)
            for i in range(len(in_splits))]
        eval_loader_names += ['env{}_out'.format(i)
            for i in range(len(out_splits))]
        eval_loader_names += ['env{}_uda'.format(i)
            for i in range(len(uda_splits))]

    algorithm_class = algorithms.get_algorithm_class(args.algorithm)
    # print(dataset.input_shape)
    algorithm = algorithm_class(dataset.input_shape, dataset.num_classes,
        len(dataset) - len(args.test_envs) - len(args.val_envs), hparams)

    if algorithm_dict is not None:
        algorithm.load_state_dict(algorithm_dict)

    algorithm.to(device)

    train_minibatches_iterator = zip(*train_loaders)
    uda_minibatches_iterator = zip(*uda_loaders)
    checkpoint_vals = collections.defaultdict(lambda: [])

    steps_per_epoch = min([len(env)/hparams['batch_size'] for env,_ in in_splits if env is not None])

    n_steps = args.steps or dataset.N_STEPS
    checkpoint_freq = args.checkpoint_freq or dataset.CHECKPOINT_FREQ

    def save_checkpoint(filename):
        if args.skip_model_save:
            return
        save_dict = {
            "args": vars(args),
            "model_input_shape": dataset.input_shape,
            "model_num_classes": dataset.num_classes,
            "model_num_domains": len(dataset) - len(args.test_envs) - len(args.val_envs),
            "model_hparams": hparams,
            "model_dict": algorithm.cpu().state_dict()
        }
        torch.save(save_dict, os.path.join(args.output_dir, filename))


    last_results_keys = None
    for step in range(start_step, n_steps):
        step_start_time = time.time()
        minibatches_device = [(x.to(device), y.to(device))
            for x,y in next(train_minibatches_iterator)]
        if args.task == "domain_adaptation":
            uda_device = [x.to(device)
                for x,_ in next(uda_minibatches_iterator)]
        else:
            uda_device = None
        # print(minibatches_device[0][0].shape,minibatches_device[0][1].shape)
        step_vals = algorithm.update(minibatches_device, uda_device)
        checkpoint_vals['step_time'].append(time.time() - step_start_time)

        for key, val in step_vals.items():
            checkpoint_vals[key].append(val)

        if (step % checkpoint_freq == 0) or (step == n_steps - 1):
            results = {
                'step': step,
                'epoch': step / steps_per_epoch,
            }

            for key, val in checkpoint_vals.items():
                results[key] = np.mean(val)

            evals = zip(eval_loader_names, eval_loaders, eval_weights)
            for name, loader, weights in evals:
                # print(f'{name}: {len(loader)}')
#                 if args.dataset in ('NICOAnimal', 'NICOVehicle'):
#                     acc, roc = misc.accuracy(algorithm, loader, weights, device, report_time=(not step), compute_roc=True)
#                     results[name+'_roc'] = roc
#                 else:
                acc = misc.accuracy(algorithm, loader, weights, device, report_time=(not step))
                results[name+'_acc'] = acc

            results_keys = sorted(results.keys())
            if results_keys != last_results_keys:
                misc.print_row(results_keys, colwidth=12)
                last_results_keys = results_keys
            misc.print_row([results[key] for key in results_keys],
                colwidth=12)

            results.update({
                'hparams': hparams,
                'args': vars(args)
            })

            epochs_path = os.path.join(args.output_dir, 'results.jsonl')
            with open(epochs_path, 'a') as f:
                f.write(json.dumps(results, sort_keys=True) + "\n")

            algorithm_dict = algorithm.state_dict()
            start_step = step + 1
            checkpoint_vals = collections.defaultdict(lambda: [])

            if args.save_model_every_checkpoint:
                save_checkpoint(f'model_step{step}.pkl')

    save_checkpoint('model.pkl')

    with open(os.path.join(args.output_dir, 'done'), 'w') as f:
        f.write('done')
