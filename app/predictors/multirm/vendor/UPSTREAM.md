# Vendored MultiRM

- Source: https://github.com/Tsedao/MultiRM (MIT licence, see `LICENSE` in this directory)
- Cloned: 2026-08-31 (default branch, unmodified clone used to produce
  `tests/fixtures/golden_multirm_151nt`)
- Checkpoint: `Weights/MultiRM/trained_model_51seqs.pkl`
  (sha256 `61f6d72aa1094ada4516bb68086d85c3d66cdac0d7deb3fd33905fa2339f608b`)

## What was copied

| Upstream file | Vendored as | Status |
|---|---|---|
| `Scripts/models.py` (class `model_v3` only) | `models.py` | modified (see below) |
| `Scripts/util_layers.py` (`BahdanauAttention`, `EmbeddingSeq`) | `layers.py` | modified (see below) |
| `Scripts/util_att.py` (`cal_attention`) + `Scripts/util_funs.py` (`highest_x`, `highest_score`) | `attention_utils.py` | `cal_attention` vectorised; the other two verbatim |
| `Weights/MultiRM/trained_model_51seqs.pkl` | `../weights/trained_model_51seqs.pkl` | byte-identical copy |
| `Embeddings/embeddings_12RM.pkl` | `../weights/embeddings_12RM.pkl` | byte-identical copy |
| `Scripts/neg_prob.csv` | `../weights/neg_prob.csv` | byte-identical copy |
| `LICENSE` | `LICENSE` | verbatim |

`Scripts/main.py` (the CLI) is **not** vendored; `app/predictors/multirm/predictor.py`
re-implements its logic as a load-once class. Everything else upstream (training code,
`NaiveNet*`, `EmbeddingHmm`, `MultiTaskLossWrapper`, `seq2index`/`mapfun`, `evaluate`,
`visualize`, notebooks) is dropped.

## Modifications vs upstream

### `models.py` (`model_v3`)
1. `EmbeddingSeq` is constructed from an `embedding_weights` tensor passed to
   `model_v3.__init__` instead of unpickling the hard-coded relative path
   `../Embeddings/embeddings_12RM.pkl`.
2. **Batch bug fix.** Upstream does `h_n = h_n.view(batch_size, output.size()[-1])` with
   `h_n` of shape `(2, batch, 256)`. This is only correct for `batch == 1`; for larger
   batches it interleaves samples across the two LSTM directions. Replaced by
   `h_n.permute(1, 0, 2).reshape(batch_size, -1)`, which is identical for batch 1 and
   gives the per-sample `[forward, backward]` concatenation for any batch size.
   Verified: batch 1 vs batch 256 probabilities agree to 3e-7 (float32 reduction-order noise).
3. `forward` returns `(probs, attention_weights)` from one pass; upstream returned only the
   list of head outputs and `util_att.evaluate` re-ran embedding + LSTM + attention to get
   the weights.
4. Head outputs are stacked into one `(batch, num_task)` tensor instead of a Python list.
5. No `.cuda()`. `nn.Sequential` layout of the heads (including the eval-time no-op
   `Dropout`) is preserved so `state_dict` keys (`NaiveFC{i}.0.*`, `NaiveFC{i}.3.*`) match.

### `layers.py`
1. `EmbeddingSeq.__init__(weights)` takes the `(num_embeddings, 300)` matrix directly; no
   pickle, no `.cuda()`.
2. `EmbeddingSeq.forward` uses `x.long()` instead of `x.type(torch.cuda.LongTensor)`.
3. `BahdanauAttention` unchanged (formatting/type hints only).

### `attention_utils.py`
1. `cal_attention(attention_weights)` takes a numpy `(batch, length, num_class)` array
   (not a torch tensor) and is vectorised over batch and class. The float32 sums are
   evaluated in the same left-to-right order as upstream's scalar loop
   (`(a[i-2] + a[i-1]) + a[i]`, missing terms are exact zeros), so the result is
   bit-identical to `cal_attention_every_class`.
2. `highest_score`, `highest_x` ported verbatim (black-style formatting only).

### Replaced by `predictor.py`
- `seq2index`/`mapfun` (which called `list(dict.keys())` per k-mer, O(vocabulary) each)
  are replaced by a 64-entry 3-mer -> token-index lookup table built once from the
  embedding dict's key order, plus a strided `(N-50, 49)` window matrix.
- Per-window `for` loop with batch-1 forward passes is replaced by chunked batched
  inference under `torch.inference_mode()`.
- `p_value = np.sum(neg_prob.iloc[k, :] > y_prob[k]) / len(bool)` is computed with a
  `searchsorted` on the pre-sorted negative rows: `(150 - #negatives <= p) / 150`, which
  equals the strict-greater count. `neg_prob.csv` is read with the identical pandas call
  (`header=None, index_col=0`); the file's last row has 149 values and an empty trailing
  field, which pandas reads as NaN. Upstream's `NaN > p` is False while the denominator
  stays 150, so that NaN is mapped to `-inf` to reproduce the same p-values exactly.
- Attention masks are computed only for windows with at least one significant site (the
  result is identical because upstream only writes `attention[...] = 1` for those).
- Upstream indexes the top-3 attention windows unconditionally (`position_dict[j]` for
  `j in 1..3`); the port guards with `min(3, len(result))`. For 51-nt windows with
  `w=3, p=1` the guard never triggers.

## Not changed
- Model architecture, weights, embeddings, negative background, window size (51), 3-mer
  vocabulary order, p-value definition, alpha semantics, attention window/top-k (3/3),
  1-based position reporting (`pos + 26`).
