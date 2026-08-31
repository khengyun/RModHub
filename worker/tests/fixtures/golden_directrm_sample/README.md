# Golden DirectRM output for the synthetic sample

Site-level tables produced by the **five unmodified upstream DirectRM scripts, run by hand**
(not through `rmodhub_worker`) on the committed synthetic sample `app/samples/signal/`
(RNA004, 88 reads, 3 transcripts; `MANIFEST.json` sha256s are copied into `meta.json`).
`worker/tests/test_golden_directrm.py` runs the worker pipeline on the same sample and
requires its `sites` table to match these files exactly (`count`, `coverage`) or within
1e-6 (`max_prob`, `noisyor_prob`); `rate` and the Wilson interval are derived in the test.

## Files

| file | content |
|---|---|
| `ac4c.csv` … `psi.csv` | `read2site.py` output per type: `seqnames,pos,strand,max_prob,noisyor_prob,count,coverage` (725 sites in total) |
| `reads.txt` | `sampling.py` output (76 read ids, in upstream's `set()` order under `PYTHONHASHSEED=0`) |
| `meta.json` | counts (reads sampled, k-mers, sites, de novo fraction), sha256 of every file above, versions, commands' parameters |

## Provenance

Generated on 2026-08-31 with a scratch py3.9 environment mirroring the original (Python 3.9)
`worker/uv.lock`:
Python 3.9.25, ont-remora 3.2.0, pod5 0.3.35, lib-pod5 0.3.35, pysam 0.24.0, torch 2.8.0+cpu,
numpy 2.0.2, pandas 2.3.3. DirectRM commit `bc7a08573dfe7629e808256fa6ade6e4111ed1f9`
(the `worker/directrm_vendor` tree: scripts byte-identical to upstream, weights re-serialised
to CPU — see `directrm_vendor/UPSTREAM.md`).

Inputs were copied into a scratch job directory with the upstream names
(`input/input.pod5`, `input/input_sorted.bam` + `.bai`, `input/reference.fa`, `input/regions.csv`).
Commands (cwd = `worker/directrm_vendor`, `<J>` = job dir, `<V>` = vendor root):

```
export PYTHONPATH=<V> PYTHONHASHSEED=0 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
python scripts/sampling.py --bam <J>/input --reg <J>/input/regions.csv -o <J>/work/reads.txt \
    --splits input --min_coverage 30 --max_coverage 150
python scripts/feature_extraction.py --pod5_dir <J>/input --bam <J>/input --reg <J>/input/regions.csv \
    --level <V>/9mer_levels_v1.txt -o <J>/work/features --splits input --read_ids <J>/work/reads.txt --kmer 9 --step 5
python scripts/denovo_inference.py --feature_dir <J>/work/features --outdir <J>/work/denovo \
    --model_path <V>/model/RNA004/id3_binary/model.pt --splits input --device cpu
python scripts/inference.py --feature_dir <J>/work/features --outdir <J>/work/inference --device cpu \
    --splits input --ml True --model_dir <V>/model/RNA004 --model_id 5
python scripts/read2site.py --indir <J>/work/inference --outdir <J>/work/sites --delete False
```

Observed: `sampling.py` -> 76 read ids (tx_A 40 + tx_B 36; tx_C with 12 reads dropped),
`feature_extraction.py` -> `0 failed`, `stat` shape (3648, 9, 8) = 3648 k-mers,
`denovo` fraction >= 0.5 = 0.00987, `inference` -> 14027 read-level rows, `read2site` -> 725 sites
(ac4c 123, m1a 120, m5c 123, m6a 115, m7g 126, psi 118). Wall time by hand: sampling 1.3 s,
features 6.6 s, denovo 1.0 s, inference 1.1 s, read2site 0.3 s.

## Determinism

The whole run was repeated a second time with the same environment: `reads.txt`, every
read-level CSV, `input_denovo.npy` and all six site tables were byte-identical. The only
run-to-run variability upstream has is the order of `set()` iteration, which depends on
`PYTHONHASHSEED`: with `PYTHONHASHSEED=1` the same 76 read ids come out in a different order.
Because DirectRM's LSTMs are built with `batch_first=False`, a different k-mer order changes
the predictions, so the worker pins `PYTHONHASHSEED=0` for every child process and runs it with
`RMODHUB_WORKER_THREADS` OMP/MKL threads: 1 in the image and in the test-suite (the count this
fixture was made with; `tests/conftest.py` pins it), 4 under `docker-compose.yml`, which keeps
every `count` / `coverage` but shifts `max_prob` / `noisyor_prob` by up to 6e-8 — within the 1e-6
tolerance, not byte-identical. Python's string hash algorithm is part of that contract:
CPython 3.9 and 3.10 both use siphash24 (`sys.hash_info.algorithm`), 3.11 switched the default
to siphash13, so re-verify (and if needed regenerate) this fixture whenever the worker moves to
another Python minor version.

## Verified on Python 3.10 (2026-08-31)

The worker moved from CPython 3.9.25 to 3.10.20 (lib-pod5 0.3.35 -> 0.3.47 for POD5 v6; every
other pin unchanged, see `worker/pyproject.toml`). The fixture was **not** regenerated: the
worker pipeline on 3.10 reproduces `reads.txt` and all six site tables byte for byte
(`tests/test_golden_directrm.py`, sha256 digests in `meta.json` unchanged, e.g. `ac4c.csv`
`e7cd987b…c914dd`, `reads.txt` `5ee72e86…000487`), as expected from the identical hash algorithm
and identical torch/numpy/pandas builds. `meta.json.versions` therefore still records the 3.9
environment the fixture was produced in.
