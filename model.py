"""Main container for common language model"""
import torch
import torch.nn as nn

from utils import get_mask

class RNNModel(nn.Module):
    """Container module with an encoder, a recurrent module, and a criterion (decoder and loss function)."""

    def __init__(self, ntoken, ninp, nhid, nlayers, criterion, dropout=0.5):
        super(RNNModel, self).__init__()
        self.drop = nn.Dropout(dropout)
        self.encoder = nn.Embedding(ntoken, ninp)
        self.rnn = nn.LSTM(ninp, nhid, nlayers, dropout=dropout, batch_first=True)
        # Usually we use the same # dim in both input and output embedding
        self.proj = nn.Linear(nhid, ninp)

        self.nhid = nhid
        self.nlayers = nlayers
        self.criterion = criterion

        self.reset_parameters()
        self.layers = []
        self.layers.append(Layer1(self.drop, self.encoder))
        self.layers.append(Layer3(self.rnn))
        self.layers.append(self.proj)
        self.layers.append(Layer2(self.criterion))

    def reset_parameters(self):
        init_range = 0.1
        self.encoder.weight.data.uniform_(-init_range, init_range)

    def _rnn(self, input):
        '''Serves as the encoder and recurrent layer'''
        emb = self.drop(self.encoder(input))
        output, unused_hidden = self.rnn(emb)
        output = self.proj(output)
        output = self.drop(output)
        return output


    def forward(self, input, target, length):

        mask = get_mask(length.data, max_len=input.size(1))
        rnn_output = self._rnn(input)
        loss = self.criterion(target, rnn_output)
        loss = torch.masked_select(loss, mask)

        return loss.mean()


class Layer1(nn.Module):

    def __init__(self, drop, encoder):
        super().__init__()
        self.drop = drop
        self.encoder = encoder

    def forward(self, input, *args):

        emb = self.drop(self.encoder(input))
        return emb


class Layer3(nn.Module):

    def __init__(self, rnn):
        super().__init__()
        self.rnn = rnn

    def forward(self, inp, *args):
        rnn_output, unused_hidden = self.rnn(inp)
        return rnn_output


class Layer2(nn.Module):

    def __init__(self, criterion):
        super().__init__()
        self.criterion = criterion

    def forward(self, input, target, length):

        mask = get_mask(length.data, max_len=input.size(1))
        rnn_output = input
        loss = self.criterion(target, rnn_output)
        loss = torch.masked_select(loss, mask)

        return loss.mean()
