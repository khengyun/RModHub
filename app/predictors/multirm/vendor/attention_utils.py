"""Attention post-processing ported from upstream `Scripts/util_att.py` and `Scripts/util_funs.py`.

- `cal_attention`: unwraps per-3-mer attention weights to per-nucleotide weights. Vectorised
  over batch and class, but evaluates the float32 sums in the same left-to-right order as
  upstream's scalar loop, so the result is bit-identical.
- `highest_score`, `highest_x`: pure-numpy top-k window search, ported verbatim
  (formatting only). They operate on one (length,) float64 row.
"""

from __future__ import annotations

import numpy as np


def cal_attention(attention_weights: np.ndarray) -> np.ndarray:
    """Unwrap 3-mer attention weights and sum them per nucleotide.

    Inputs:  attention weights of shape (batch, length, num_class), float32
             (`length` is the number of 3-mers, i.e. 49 for a 51-nt window).
    Outputs: per-nucleotide weights of shape (batch, num_class, length + 2), float64.

    Nucleotide i is covered by the 3-mers starting at i-2, i-1 and i (those that exist), so
    its weight is `a[i-2] + a[i-1] + a[i]` with out-of-range terms treated as 0, exactly as
    in upstream `cal_attention_every_class`.
    """
    a = np.asarray(attention_weights, dtype=np.float32)
    if a.ndim != 3:
        raise ValueError(f"expected (batch, length, num_class), got shape {a.shape}")
    a = np.transpose(a, (0, 2, 1))  # (batch, num_class, length)
    padded = np.pad(a, ((0, 0), (0, 0), (2, 2)))  # two zero k-mers on each side
    # (a[i-2] + a[i-1]) + a[i], in float32, same association order as the upstream loop.
    per_nt = (padded[..., :-2] + padded[..., 1:-1]) + padded[..., 2:]
    return per_nt.astype(np.float64)


def highest_score(a, w):
    """
    Inputs:
        a: a 1-D numpy array contains the scores of each position
        w: length of window to aggregate the scores
    """

    assert len(a) >= w

    best = -20000
    best_idx_start = 0
    best_idx_end = 0
    for i in range(len(a) - w + 1):
        tmp = np.sum(a[i : i + w])
        if tmp > best:
            best = tmp
            best_idx_start = i
            best_idx_end = i + w - 1

    return best, best_idx_start, best_idx_end


def highest_x(a, w, p=1):
    """
    Inputs:
        a: a 1-D numpy array contains the scores of each position
        w: length of window to aggregate the scores
        p: length of padding when maximum sum of consecutive numbers are taken
    Returns:
        {1: (score, start, end), 2: ..., ...} ranked windows of width `w` (inclusive
        start/end indices into `a`). After each pick the window plus `p` nt of padding on
        each side is removed from further consideration.
    """

    lists = [{k: v for (k, v) in zip(range(len(a)), a)}]
    result = {}
    max_idx = len(a) - 1
    count = 1
    condition = [True]
    while any(con is True for con in condition):
        starts = []
        ends = []
        bests = []

        for ele in lists:
            values = list(ele.values())
            idx = list(ele.keys())

            start_idx = idx[0]

            if len(values) >= w:
                highest, highest_idx_start, highest_idx_end = highest_score(values, w)

                starts.append(highest_idx_start + start_idx)

                ends.append(highest_idx_end + start_idx)

                bests.append(highest)

        best_idx = max(zip(bests, range(len(bests))))[1]  # calculate the index of maximum sum

        cut_value = bests[best_idx]

        if starts[best_idx] - p >= 0:
            cut_idx_start = starts[best_idx] - p
        else:
            cut_idx_start = 0

        if ends[best_idx] + p <= max_idx:
            cut_idx_end = ends[best_idx] + p
        else:
            cut_idx_end = max_idx

        result[count] = (cut_value, starts[best_idx], ends[best_idx])

        copy = lists.copy()

        for ele in lists:
            values = list(ele.values())
            idx = list(ele.keys())

            start_idx, end_idx = idx[0], idx[-1]

            if len(values) < w:
                copy.remove(ele)
            else:
                if (cut_idx_end < start_idx) or (cut_idx_start > end_idx):
                    pass
                elif (cut_idx_start < start_idx) and (cut_idx_end >= start_idx):
                    copy.remove(ele)
                    values = values[cut_idx_end - start_idx + 1 :]
                    idx = idx[cut_idx_end - start_idx + 1 :]
                    ele = {k: v for (k, v) in zip(idx, values)}

                    if ele != {}:
                        copy.append(ele)

                elif (cut_idx_start >= start_idx) and (cut_idx_end <= end_idx):
                    copy.remove(ele)
                    values_1 = values[: cut_idx_start - start_idx]
                    idx_1 = idx[: cut_idx_start - start_idx]
                    ele_1 = {k: v for (k, v) in zip(idx_1, values_1)}

                    values_2 = values[cut_idx_end - start_idx + 1 :]
                    idx_2 = idx[cut_idx_end - start_idx + 1 :]
                    ele_2 = {k: v for (k, v) in zip(idx_2, values_2)}

                    if ele_1 != {}:
                        copy.append(ele_1)
                    if ele_2 != {}:
                        copy.append(ele_2)

                elif (cut_idx_start <= end_idx) and (cut_idx_end > end_idx):
                    copy.remove(ele)
                    values = values[: cut_idx_start - start_idx]
                    idx = idx[: cut_idx_start - start_idx]
                    ele = {k: v for (k, v) in zip(idx, values)}

                    if ele != {}:
                        copy.append(ele)

        lists = copy
        count = count + 1
        condition = [len(i) >= w for i in lists]

    return result
