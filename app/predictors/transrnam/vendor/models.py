"""TransRNAm network, transcribed from upstream for CPU inference.

Upstream: https://github.com/lennylv/TransRNAm (`Scripts/models.py`, `Scripts/util_layers.py`,
`Scripts/train_utils.py`). Layer shapes and the forward order are kept exactly as published so
`Optimal_Model/Best_weights.pkl` loads with `strict=True`; see `UPSTREAM.md` for the deviations.
"""

from __future__ import annotations

import copy
import math

import torch
import torch.nn.functional as F
from torch import nn

EMBEDDING_DIM = 300
N_HEADS = 6
TRANSFORMER_OUT = 512
CNN_OUT = 256
N_TASKS = 12


class InputEmbeddings(nn.Module):
    """Frozen Word2Vec 3-mer table (`embeddings_12RM.pkl`, the file MultiRM also uses)."""

    def __init__(self, num_embeddings: int, embedding_dim: int = EMBEDDING_DIM):
        super().__init__()
        self.embedding = nn.Embedding(num_embeddings, embedding_dim)
        self.embedding.weight.requires_grad = False

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.embedding(x.long())


class PositionalEncoding(nn.Module):
    def __init__(self, embedding_dim: int, dropout: float, max_len: int = 5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)
        pe = torch.zeros(max_len, embedding_dim)
        position = torch.arange(0.0, max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0.0, embedding_dim, 2) * -(math.log(10000.0) / embedding_dim)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        # Registered as a buffer upstream too, so `pe` is part of the checkpoint.
        self.register_buffer("pe", pe.unsqueeze(0))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.dropout(x + self.pe[:, : x.size(1)])


def _clones(module: nn.Module, n: int) -> nn.ModuleList:
    return nn.ModuleList([copy.deepcopy(module) for _ in range(n)])


class MultiHeadedAttention(nn.Module):
    def __init__(self, h: int, embedding_dim: int, dropout: float = 0.1):
        super().__init__()
        assert embedding_dim % h == 0
        self.d_k = embedding_dim // h
        self.h = h
        self.linears = _clones(nn.Linear(embedding_dim, embedding_dim), 4)
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, query, key, value):
        nbatches = query.size(0)
        query, key, value = (
            layer(x).view(nbatches, -1, self.h, self.d_k).transpose(1, 2)
            for layer, x in zip(self.linears, (query, key, value))
        )
        scores = torch.matmul(query, key.transpose(-2, -1)) / math.sqrt(query.size(-1))
        attn = torch.matmul(self.dropout(F.softmax(scores, dim=-1)), value)
        attn = attn.transpose(1, 2).contiguous().view(nbatches, -1, self.h * self.d_k)
        return self.linears[-1](attn)


class MyTransformerModel(nn.Module):
    def __init__(self, num_embeddings: int, p_drop: float = 0.2):
        super().__init__()
        self.embeddings = InputEmbeddings(num_embeddings)
        self.position = PositionalEncoding(EMBEDDING_DIM, p_drop)
        self.atten = MultiHeadedAttention(N_HEADS, EMBEDDING_DIM)
        self.norm = nn.LayerNorm(EMBEDDING_DIM)
        self.linear = nn.Linear(EMBEDDING_DIM, TRANSFORMER_OUT)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        embeded = self.position(self.embeddings(x))
        attended = self.norm(self.atten(embeded, embeded, embeded) + embeded)
        # Upstream normalises a second time before pooling; kept so the weights mean the
        # same thing they did at training time.
        attended = self.norm(attended)
        pooled = attended.sum(1) / (embeded.shape[1] + 1e-5)
        return self.linear(pooled)  # (batch, 512)


class NaiveNet(nn.Module):
    """The CNN branch, applied to the transformer output as a length-512 "signal"."""

    def __init__(self, input_size: int = TRANSFORMER_OUT):
        super().__init__()
        self.NaiveCNN = nn.Sequential(
            nn.Conv1d(1, 8, kernel_size=7, stride=2),
            nn.ReLU(),
            nn.Dropout(p=0.2),
            nn.Conv1d(8, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2),
            nn.Dropout(p=0.2),
            nn.Conv1d(32, 128, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2),
        )
        f1 = (input_size - 7) // 2 + 1
        f2 = (f1 - 2) // 2 + 1
        f3 = (f2 - 2) // 2 + 1
        self.Flatten = nn.Flatten()
        self.SharedFC = nn.Sequential(nn.Linear(128 * f3, CNN_OUT), nn.ReLU(), nn.Dropout())

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.SharedFC(self.Flatten(self.NaiveCNN(x)))


class model_v11(nn.Module):  # upstream name kept; checkpoint keys depend on attribute names
    """Transformer + CNN trunk with one sigmoid head per modification.

    `forward` takes the k-mer index windows directly. Upstream's signature is
    `forward(self, x, fp)` where `x` is a row id into a precomputed encoding table and
    `fp` that table; the lookup and the central 599-column crop are done by the caller
    here (`encoder.py`), which is what lets the server score an arbitrary sequence.
    """

    def __init__(self, num_embeddings: int, num_task: int = N_TASKS):
        super().__init__()
        self.num_task = num_task
        self.transfomer1 = MyTransformerModel(num_embeddings)
        self.cnn = NaiveNet(TRANSFORMER_OUT)
        for i in range(num_task):
            setattr(
                self,
                f"NaiveFC{i}",
                nn.Sequential(
                    nn.Linear(in_features=768, out_features=128),
                    nn.ReLU(),
                    nn.Dropout(),
                    nn.Linear(in_features=128, out_features=1),
                    nn.Sigmoid(),
                ),
            )

    def forward(self, kmer_idx: torch.Tensor) -> torch.Tensor:
        """kmer_idx: (batch, 599) Long. Returns (batch, num_task) sigmoid outputs."""
        trunk = self.transfomer1(kmer_idx)  # (batch, 512)
        conv = self.cnn(trunk.unsqueeze(1))  # (batch, 256)
        fused = torch.cat((trunk, conv), dim=-1)  # (batch, 768)
        return torch.cat(
            [getattr(self, f"NaiveFC{i}")(fused) for i in range(self.num_task)], dim=1
        )
