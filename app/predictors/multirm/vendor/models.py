"""`model_v3` from upstream `Scripts/models.py` (the network behind `trained_model_51seqs.pkl`).

Architecture: 3-mer embedding (300-d) -> BiLSTM(256, bidirectional) -> Bahdanau attention
with one head per task -> 12 independent sigmoid classifiers.

Modifications vs upstream (details in UPSTREAM.md):
- Embedding weights are injected via `embedding_weights` instead of a hard-coded path.
- `h_n.view(batch_size, 512)` replaced by `h_n.permute(1, 0, 2).reshape(batch_size, -1)`.
  The upstream `view` is only correct for batch_size == 1; for larger batches it silently
  interleaves samples across the two LSTM directions. The permute/reshape form gives
  exactly the upstream result for batch 1 and the *correct* per-sample concatenation
  [forward h_n, backward h_n] for any batch size.
- `forward` returns `(probs, attention_weights)` in a single pass; upstream's
  `util_att.evaluate` ran the network twice to get the attention weights.
- `probs` is stacked into one (batch, num_task) tensor instead of a Python list.
- No `.cuda()`; the only other classes in the upstream file (NaiveNet*) are not vendored.
"""

from __future__ import annotations

import torch
from torch import nn

from app.predictors.multirm.vendor.layers import BahdanauAttention, EmbeddingSeq


class model_v3(nn.Module):  # upstream name kept; checkpoint keys depend on attribute names
    def __init__(
        self,
        num_task: int,
        use_embedding: bool,
        embedding_weights: torch.Tensor | None = None,
    ):
        super().__init__()

        self.num_task = num_task
        self.use_embedding = use_embedding
        if self.use_embedding:
            if embedding_weights is None:
                raise ValueError("embedding_weights is required when use_embedding=True")
            self.embed = EmbeddingSeq(embedding_weights)  # Word2Vec 3-mer embeddings
            self.NaiveBiLSTM = nn.LSTM(
                input_size=300, hidden_size=256, batch_first=True, bidirectional=True
            )
        else:
            self.NaiveBiLSTM = nn.LSTM(
                input_size=4, hidden_size=256, batch_first=True, bidirectional=True
            )

        self.Attention = BahdanauAttention(in_features=512, hidden_units=100, num_task=num_task)
        for i in range(num_task):
            # Keep the exact Sequential layout (Dropout at index 2) so the state_dict keys
            # `NaiveFC{i}.0.*` / `NaiveFC{i}.3.*` still match. Dropout is a no-op in eval().
            setattr(
                self,
                f"NaiveFC{i}",
                nn.Sequential(
                    nn.Linear(in_features=512, out_features=128),
                    nn.ReLU(),
                    nn.Dropout(),
                    nn.Linear(in_features=128, out_features=1),
                    nn.Sigmoid(),
                ),
            )

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Return `(probs, attention_weights)`.

        x: (batch, 49) Long k-mer indices when `use_embedding`, else (batch, 4, L) one-hot.
        probs: (batch, num_task) sigmoid outputs, task order == upstream RMs order.
        attention_weights: (batch, 49, num_task) softmax over the k-mer axis.
        """
        if self.use_embedding:
            x = self.embed(x)
        else:
            x = torch.transpose(x, 1, 2)
        batch_size = x.size()[0]

        output, (h_n, _c_n) = self.NaiveBiLSTM(x)  # h_n: (2, batch, 256)
        # Batch-safe replacement for upstream `h_n.view(batch_size, 512)`; see module docstring.
        h_n = h_n.permute(1, 0, 2).reshape(batch_size, -1)  # (batch, 512)
        context_vector, attention_weights = self.Attention(h_n, output)
        outs = []
        for i in range(self.num_task):
            fc_layer = getattr(self, f"NaiveFC{i}")
            y = fc_layer(context_vector[:, i, :])
            y = torch.squeeze(y, dim=-1)
            outs.append(y)

        return torch.stack(outs, dim=1), attention_weights
