#!/usr/bin/env python

import sys
import time
import math

from tqdm import tqdm

import torch
import torch.optim as optim

import data
from model import RNNModel
from utils import process_data, build_unigram_noise, setup_parser, setup_logger
from generic_model import GenModel
from nce import IndexGRU, IndexLinear


parser = setup_parser()
args = parser.parse_args()
logger = setup_logger('{}'.format(args.save))
logger.info(args)
model_path = './saved_model/{}'.format(args.save)

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        logger.warning('You have a CUDA device, so you should probably run with --cuda')
    else:
        torch.cuda.manual_seed(args.seed)

#################################################################
# Load data
#################################################################
corpus = data.Corpus(
    path=args.data,
    vocab_path=args.vocab,
    batch_size=args.batch_size,
    shuffle=True,
    pin_memory=args.cuda,
    min_freq=args.min_freq,
    concat=args.concat,
    bptt=args.bptt,
)

ntoken = len(corpus.vocab)
logger.info('Vocabulary size is {}'.format(ntoken))

################################################################## Build the criterion and model, setup the NCE module
#################################################################

def build_model(use_pipe=True):
    """Build the model according to CLI arguments

    Global Dependencies:
        - corpus
        - args
    """
    # noise for soise sampling in NCE
    noise = build_unigram_noise(
        torch.FloatTensor(corpus.vocab.idx2count)
    )

    norm_term = 'auto' if args.norm_term == -1 else args.norm_term
    # setting up NCELoss modules
    if args.index_module == 'linear':
        criterion = IndexLinear(
            args.nhid,
            ntoken,
            noise=noise,
            noise_ratio=args.noise_ratio,
            norm_term=norm_term,
            loss_type=args.loss,
            reduction='none',
        )
        model = RNNModel(
            ntoken, args.emsize, args.nhid, args.nlayers,
            criterion=criterion, dropout=args.dropout,
        )
        if use_pipe:
            from pipeline_model import Pipeline
            model = Pipeline(model.layers)
            torch.cuda.set_device(0)
    elif args.index_module == 'gru':
        if args.nlayers != 1:
            logger.warning('Falling into one layer GRU due to Index_GRU supporting')
        nce_criterion = IndexGRU(
            ntoken, args.nhid, args.nhid,
            args.dropout,
            noise=noise,
            noise_ratio=args.noise_ratio,
            norm_term=norm_term,
        )
        model = GenModel(
            criterion=nce_criterion,
        )
    else:
        logger.error('The index module [%s] is not supported yet' % args.index_module)
        raise(NotImplementedError('index module not supported'))

    if args.cuda:
        model.cuda()

    logger.info('model definition:\n %s', model)
    return model

model = build_model()
sep_target = args.index_module == 'linear'
#################################################################
# Training code
#################################################################

optimizer = optim.Adam(
    params=model.parameters(),
    lr=1e-3,
    # momentum=momentum,
    # weight_decay=weight_decay
)

def train(model, data_source, epoch, lr=1.0, weight_decay=1e-5, momentum=0.9):
    # Turn on training mode which enables dropout.
    model.train()
    # model.criterion.loss_type = args.loss
    total_loss = 0
    pbar = tqdm(data_source, desc='Training PPL: ....', disable = model.rank != model.world_size - 1)
    num_batch = len(pbar)
    model.set_num_batch(num_batch)
    model.reset()
    torch.manual_seed(epoch)
    for num_batch, data_batch in enumerate(pbar):
        data, target, length = process_data(data_batch, cuda=args.cuda, sep_target=sep_target)

        # Construct the input data, for many workers there's no need to read
        # the real data, so we only need a place-holder None
        real_input = None
        if model.rank == 0:
            real_input = data

        # At output layer, we need some extra info to compute gradient,
        # he same as input layer
        real_extra_output = tuple()
        if model.rank == model.world_size - 1:
            real_extra_output = (target, length)

        loss = model(real_input, *real_extra_output)

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)
        optimizer.step()
        optimizer.zero_grad()

        if model.rank == model.world_size - 1:
            total_loss += loss.item()

            if args.prof:
                break
            if num_batch % args.log_interval == 0 and num_batch > 0:
                cur_loss = total_loss / args.log_interval
                ppl = math.exp(cur_loss)
                logger.debug(
                    '| epoch {:3d} | {:5d}/{:5d} batches '
                    '| lr {:02.2f} | loss {:5.2f} | ppl {:8.2f}'.format(
                        epoch, num_batch, len(corpus.train),
                        lr, cur_loss, ppl
                      )
                )
                pbar.set_description('Training PPL %.1f' % ppl)
                total_loss = 0


def evaluate(model, data_source, cuda=args.cuda):
    # Turn on evaluation mode which disables dropout.
    whole_model = build_model(use_pipe=False)
    from pipeline_model import Pipeline
    Pipeline.load_to_original_model(whole_model, model_path)
    whole_model.eval()
    whole_model.criterion.loss_type = 'full'

    eval_loss = 0
    total_length = 0

    with torch.no_grad():
        for data_batch in data_source:
            data, target, length = process_data(data_batch, cuda=cuda, sep_target=sep_target)

            loss = whole_model(data, target, length)
            cur_length = int(length.data.sum())
            eval_loss += loss.item() * cur_length
            total_length += cur_length

    whole_model.criterion.loss_type = args.loss

    return math.exp(eval_loss/total_length)


def run_epoch(epoch, lr, best_val_ppl):
    """A training epoch includes training, evaluation and logging"""
    part_name = '.part{}'.format(model.rank)
    epoch_start_time = time.time()
    train(model, corpus.train, epoch=epoch, lr=lr, weight_decay=args.weight_decay)
    import torch.distributed as dist
    dist.barrier()
    # torch.save(model.target_workload.state_dict(), model_path + part_name)
    # initial saving
    model.save(model_path)
    val_ppl = torch.Tensor([1000])
    if model.rank == model.world_size - 1:
        val_ppl[0] = evaluate(model, corpus.valid)
        logger.warning(
            '| end of epoch {:3d} | time: {:5.2f}s |'
            'valid ppl {:8.2f}'.format(
                epoch,
                (time.time() - epoch_start_time),
                val_ppl.item())
        )
        # Save the model if the validation loss is the best we've seen so far.

    dist.broadcast(val_ppl, src=model.world_size - 1)

    if not best_val_ppl or val_ppl < best_val_ppl:
        model.save(model_path + '.best')
        best_val_ppl = val_ppl

    return lr, best_val_ppl

if __name__ == '__main__':
    lr = args.lr
    best_val_ppl = None
    if args.train:
        # At any point you can hit Ctrl + C to break out of training early.
        try:
            for epoch in range(1, args.epochs + 1):
                lr, best_val_ppl = run_epoch(epoch, lr, best_val_ppl)
                if args.prof:
                    break
        except KeyboardInterrupt:
            logger.warning('Exiting from training early')

    else:
        # Load the best saved model.
        logger.warning('Evaluating existing model {}'.format(args.save))
        # model = torch.load(model_path)

    # Run on test data.
    test_ppl = evaluate(model, corpus.test)
    logger.warning('| End of training | test ppl {:8.2f}'.format(test_ppl))
    sys.stdout.flush()
