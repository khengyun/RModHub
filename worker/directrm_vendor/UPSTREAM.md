# Vendored DirectRM

* **Source**: https://github.com/yuxinPenny/DirectRM
* **Commit**: `bc7a08573dfe7629e808256fa6ade6e4111ed1f9`
* **Licence**: MIT, (c) 2025 Yuxin Zhang (`LICENSE` in this directory). Paper: Zhang et al.,
  *Nat Commun* 16, 9450 (2025), https://doi.org/10.1038/s41467-025-64495-8
* The k-mer level tables (`5mer_levels_v1.txt`, `9mer_levels_v1.txt`) are byte-identical to ONT
  `kmer_models` (`rna_r9.4_180mv_70bps/5mer_levels_v1.txt`, `rna004/9mer_levels_v1.txt`),
  licensed MPL-2.0. The Remora runtime dependency (`ont-remora`) is under the Oxford Nanopore
  Technologies Public License 1.0 (research use only); both are disclosed on the landing page.

## What was copied verbatim

Byte-identical copies (sha256 in `WEIGHTS_MANIFEST.json` under `"verbatim"`):

| path | role |
|---|---|
| `scripts/sampling.py` | stage `sampling`: read ids per region (`--min_coverage`/`--max_coverage`) |
| `scripts/feature_extraction.py` | stage `features`: Remora re-squiggle + 9-mer features (`{split}.npz`, `{split}.csv`) |
| `scripts/denovo_inference.py` | stage `denovo`: binary "modified k-mer" probability (`{split}_denovo.npy`) |
| `scripts/inference.py` | stage `inference`: 6-type model, read-level CSVs per type/seqname |
| `scripts/read2site.py` | stage `aggregating`: site-level tables per type |
| `utils/dataset.py`, `utils/model.py`, `utils/loss.py` | model code (`loss.py` is training-only, kept for completeness) |
| `5mer_levels_v1.txt`, `9mer_levels_v1.txt` | Remora level tables (RNA002 / RNA004) |
| `LICENSE` | MIT |

Not vendored: `README.md`, `DirectRM.yml`, `remora-env.yml`, `figure_reproduce.Rmd`, `.gitattributes`,
`.DS_Store`, `__pycache__/` and `model/RNA004/ac4c_m6/train_perf.txt` (a stray training curve).

## Weights: `model/RNA002/**` and `model/RNA004/**`

All 106 `model.pt` files are `OrderedDict` state dicts of float32 tensors. The upstream files were
saved from GPU tensors and carry `cuda:0` storage tags, and the upstream scripts call
`torch.load(path)` **without** `map_location` (`denovo_inference.py:66`, `inference.py:128-136`).
On a CPU-only machine that raises
`RuntimeError: Attempting to deserialize object on a CUDA device but torch.cuda.is_available() is False`.

Because the scripts must stay unmodified, every weight file was re-serialised once at vendoring
time with

```python
torch.save(torch.load(p, map_location="cpu"), q)
```

and verified: the reloaded state dict has the same keys in the same order and `torch.equal`
holds for every tensor (`n_tensors` per file in the manifest). `WEIGHTS_MANIFEST.json` records
`original_sha256` (upstream file), `vendored_sha256` (file in this tree), `n_tensors` and
`verified_equal` for each of the 106 files, plus the torch version used (2.8.0+cpu). Numerical
values are unchanged; only the pickle's storage-location tag differs.

Reproduce with a clone of the upstream repository:

```
uv run --project worker python worker/scripts/vendor_directrm.py /path/to/DirectRM
```

(The script refuses to run on any commit other than the one above unless `--expect-commit ''`.)

## How the scripts are invoked (see `docs/signal-branch.md` section 2)

The worker (`rmodhub_worker/pipeline.py`) runs each script as a subprocess with

* `cwd` = this directory, `PYTHONPATH` = this directory (the scripts import `utils.*` as a
  namespace package and do no `sys.path` handling themselves);
* `PYTHONHASHSEED=0` (upstream orders reads through `set()` iteration; the models are
  batch-order sensitive, so the hash seed is pinned for reproducibility);
* `OMP_NUM_THREADS=MKL_NUM_THREADS=<RMODHUB_WORKER_THREADS>` (default 1);
* `--device cpu`, split name `input`, `--kmer 9 --step 5`, `--ml True --model_id 5`,
  `--delete False` for `read2site.py`;
* level table `9mer_levels_v1.txt` for `RNA004`, `5mer_levels_v1.txt` for `RNA002`; model dir
  `model/<KIT>`; de novo model `model/<KIT>/id3_binary/model.pt`.

## Upstream behaviours the worker handles (never "fixed" here)

* `_denovo.npy` is computed but not consumed by `inference.py` (the filter lines are commented
  out). The worker stores `denovo_frac_modified` (fraction of k-mers with p >= 0.5) in `meta`
  and does not gate inference with it.
* An empty feature file (0 k-mers) or a dwell column with std == 0 crashes stages 3/4
  (`reshape` of a 0-length array / NaN after z-scoring). The worker checks `features/input.npz`
  right after stage 2 and fails with a clear message.
* `sampling.py` drops regions with <= `min_coverage` reads and randomly subsamples regions with
  >= `max_coverage` reads with an unseeded `random.sample`. The worker counts reads per region
  itself (same Remora `fetch` semantics: every alignment record on the requested strand) and
  reports `regions_skipped_low_coverage` / `regions_subsampled` in `meta`.
* Coordinates: `regions.csv` is 1-based inclusive (`seqnames,start,end,width,strand`); output
  `pos` is 1-based. Upstream never scores the first base of each region and k-mers may extend
  up to 8 bases past `end`.
* `read2site` `coverage` is the number of reads with a non-zero score at that base for that type
  (not raw read depth); `count` is the number of reads with score > 0.5.
* `inference.py` appends to existing output CSVs; the worker always uses a fresh
  `work/inference/` directory. With more than 200 seqnames per split it writes
  `<type>/<file_id>.csv` buckets plus `metadata.json`; the worker reads both layouts.
* Every `nn.LSTM` is built with `batch_first=False`, so predictions depend on the order and
  composition of the 512-row batches; with `PYTHONHASHSEED=0` and a fixed read set the output is
  reproducible (verified byte-identical across runs).
* `feature_extraction.py` calls `Read.from_pod5_and_alignment` without `reverse_signal=True`; on
  real dorado RNA output the per-base signal is therefore mirrored within a read relative to
  Remora's own RNA convention. The worker reproduces upstream faithfully.
