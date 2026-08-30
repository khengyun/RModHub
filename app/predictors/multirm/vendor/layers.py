"""Layers used by `model_v3` (ported from upstream `Scripts/util_layers.py`).

Modifications vs upstream:
- `EmbeddingSeq` takes the embedding matrix as a tensor instead of unpickling a
  hard-coded relative path, and never calls `.cuda()`.
- `EmbeddingSeq.forward` casts with `x.long()` instead of `torch.cuda.LongTensor`.
- `EmbeddingHmm` and `MultiTaskLossWrapper` (training-only) are not vendored.
`BahdanauAttention` is unchanged apart from formatting.
"""

from __future__ import annotations

import torch
from torch import nn


class BahdanauAttention(nn.Module):
    """Additive attention with one output head per task.

    input: from RNN module h_1, ... , h_n (batch_size, seq_len, units*num_directions),
                                    h_n: (num_directions, batch_size, units)
    return: (batch_size, num_task, units)
    """

    def __init__(self, in_features: int, hidden_units: int, num_task: int):
        super().__init__()
        self.W1 = nn.Linear(in_features=in_features, out_features=hidden_units)
        self.W2 = nn.Linear(in_features=in_features, out_features=hidden_units)
        self.V = nn.Linear(in_features=hidden_units, out_features=num_task)

    def forward(self, hidden_states: torch.Tensor, values: torch.Tensor):
        hidden_with_time_axis = torch.unsqueeze(hidden_states, dim=1)

        score = self.V(nn.Tanh()(self.W1(values) + self.W2(hidden_with_time_axis)))
        attention_weights = nn.Softmax(dim=1)(score)  # (batch, seq_len, num_task)
        values = torch.transpose(values, 1, 2)  # (batch, units, seq_len) for matmul
        context_vector = torch.matmul(values, attention_weights)  # (batch, units, num_task)
        context_vector = torch.transpose(context_vector, 1, 2)  # (batch, num_task, units)
        return context_vector, attention_weights


class EmbeddingSeq(nn.Module):
    """Frozen k-mer embedding lookup.

    `weights` is the (num_embeddings, embedding_dim) matrix built from the upstream
    `embeddings_12RM.pkl` dictionary, rows in dictionary key order.
    """

    def __init__(self, weights: torch.Tensor):
        super().__init__()
        weights = torch.as_tensor(weights, dtype=torch.float32)
        num_embeddings, embedding_dim = weights.shape

        self.embedding = nn.Embedding(num_embeddings=num_embeddings, embedding_dim=embedding_dim)
        self.embedding.weight = nn.Parameter(weights)
        self.embedding.weight.requires_grad = False

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.embedding(x.long())
