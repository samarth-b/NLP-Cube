import torch
import random
import torch.nn as nn
import numpy as np
from cube2.networks.self_attention import SelfAttentionNetwork
from cube2.networks.modules import Encoder
from cube.io_utils.encodings import Encodings
from cube2.config import TaggerConfig


class TextEncoder(nn.Module):
    config: TaggerConfig
    encodings: Encodings

    def __init__(self, config, encodings, ext_conditioning=None, target_device='cpu'):
        super(TextEncoder, self).__init__()
        self.encodings = encodings
        self.config = config
        self.use_conditioning = (ext_conditioning is None)
        if ext_conditioning is None:
            ext_conditioning = 0
        self._target_device = target_device

        self.encoder = Encoder('float', self.config.tagger_embeddings_size + ext_conditioning,
                               self.config.tagger_embeddings_size,
                               self.config.tagger_encoder_size,
                               self.config.tagger_encoder_size, self.config.tagger_encoder_dropout, nn_type=nn.LSTM,
                               num_layers=self.config.tagger_encoder_layers)
        self.character_network = SelfAttentionNetwork('float', self.config.char_input_embeddings_size,
                                                      self.config.char_input_embeddings_size,
                                                      self.config.char_encoder_size, self.config.char_encoder_layers,
                                                      self.config.tagger_embeddings_size,
                                                      self.config.tagger_encoder_dropout, nn_type=nn.LSTM)

        mlp_input_size = self.config.tagger_encoder_size * 2 + ext_conditioning
        self.mlp = nn.Sequential(nn.Linear(mlp_input_size, self.config.tagger_mlp_layer, True),
                                 nn.Tanh(),
                                 nn.Dropout(p=self.config.tagger_mlp_dropout))

        self.word_emb = nn.Embedding(len(self.encodings.word2int), self.config.tagger_embeddings_size, padding_idx=0)
        self.char_emb = nn.Embedding(len(self.encodings.char2int), self.config.char_input_embeddings_size,
                                     padding_idx=0)
        self.case_emb = nn.Embedding(4, 16,
                                     padding_idx=0)

        self.char_proj = nn.Sequential(
            nn.Linear(self.config.char_input_embeddings_size + 16, self.config.char_input_embeddings_size),
            nn.Tanh(),
            nn.Dropout(p=self.config.tagger_encoder_dropout))

    def forward(self, x, conditioning=None):
        char_network_batch, word_network_batch = self._create_batches(x)
        char_network_output = self.character_network(char_network_batch)
        word_emb = self.word_emb(word_network_batch)
        char_emb = char_network_output.view(word_emb.size())
        if self.training:
            masks_char, masks_word = self._compute_masks(char_emb.size(), self.config.tagger_input_dropout_prob)
            x = torch.tanh(
                masks_char.unsqueeze(2) * char_emb + masks_word.unsqueeze(2) * word_emb)
        else:
            x = torch.tanh(char_emb + word_emb)
        output, _ = self.encoder(x.permute(1, 0, 2))
        return self.mlp(output.permute(1, 0, 2))

    def _compute_masks(self, size, prob):
        m1 = np.ones(size[:-1])
        m2 = np.ones(size[:-1])

        for ii in range(m1.shape[0]):
            for jj in range(m2.shape[1]):
                p1 = random.random()
                p2 = random.random()
                if p1 >= prob and p2 < prob:
                    mm1 = 2
                    mm2 = 0
                elif p1 < prob and p2 >= prob:
                    mm1 = 0
                    mm2 = 2
                elif p1 < prob and p2 < prob:
                    mm1 = 0
                    mm2 = 0
                else:
                    mm1 = 1
                    mm2 = 1
                m1[ii, jj] = mm1
                m2[ii, jj] = mm2
        device = self._get_device()
        return torch.tensor(m1, dtype=torch.float32, device=device), torch.tensor(m2, dtype=torch.float32,
                                                                                  device=device)

    @staticmethod
    def _case_index(char):
        if char.lower() == char.upper():  # symbol
            return 3
        elif char.upper() != char:  # lowercase
            return 2
        else:  # uppercase
            return 1

    def _get_device(self):
        return self._target_device

    def _create_batches(self, x):
        char_batch = []
        case_batch = []
        word_batch = []
        max_sent_size = 0
        max_word_size = 0

        for sent in x:
            if len(sent) > max_sent_size:
                max_sent_size = len(sent)
            for entry in sent:
                if len(entry.word) > max_word_size:
                    max_word_size = len(entry.word)
        # print(max_sent_size)
        for sent in x:
            sent_int = []

            for entry in sent:
                char_int = []
                case_int = []
                if entry.word.lower() in self.encodings.word2int:
                    sent_int.append(self.encodings.word2int[entry.word.lower()])
                else:
                    sent_int.append(self.encodings.word2int['<UNK>'])
                for char in entry.word:
                    if char.lower() in self.encodings.char2int:
                        char_int.append(self.encodings.char2int[char.lower()])
                    else:
                        char_int.append(self.encodings.char2int['<UNK>'])
                    case_int.append(self._case_index(char))
                for _ in range(max_word_size - len(entry.word)):
                    char_int.append(self.encodings.char2int['<PAD>'])
                    case_int.append(0)
                char_batch.append(char_int)
                case_batch.append(case_int)

            for _ in range(max_sent_size - len(sent)):
                sent_int.append(self.encodings.word2int['<PAD>'])
                char_batch.append([0 for _ in range(max_word_size)])
                case_batch.append([0 for _ in range(max_word_size)])
            word_batch.append(sent_int)

        device = self._get_device()
        char_batch = self.char_emb(torch.tensor(char_batch, device=device))
        case_batch = self.case_emb(torch.tensor(case_batch, device=device))

        char_emb = torch.cat([char_batch, case_batch], dim=2)
        char_batch = self.char_proj(char_emb)
        return char_batch, torch.tensor(word_batch, device=device)
