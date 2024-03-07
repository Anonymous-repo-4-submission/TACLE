import json
import argparse
from trainer import train
from evaluator import test

def main():
    args = setup_parser().parse_args() 
    param = load_json(args.config) 
    args = vars(args)  
    args.update(param)  
    if args['test_only']:
        test(args)
    else:
        train(args)

def load_json(settings_path):
    with open(settings_path) as data_file:
        param = json.load(data_file)
    return param

def setup_parser():
    parser = argparse.ArgumentParser(description='Reproduce of multiple continual learning algorthms.')
    parser.add_argument('--config', type=str, default='./exps_cifar100_0.8/slca_cifar100-imagenet_pretrained_0.8%_0Buffer_ourApproach.json',
                        help='Json file of settings.')
    parser.add_argument('--test_only', action='store_true')
    return parser

if __name__ == '__main__':
    main()