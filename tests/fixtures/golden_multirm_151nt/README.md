# Golden fixture: MultiRM reference output (151 nt, alpha=0.05)

Produced by the **unmodified upstream** `MultiRM/Scripts/main.py` (commit at clone time, 2026-08-31)
run through the CPU shim (`run_cpu.py`) with:

    python run_cpu.py -s <sequence.txt> --alpha 0.05 --save True --save_path <dir> --save_id job1

Files (all matrices: rows = 12 mod types in canonical order, columns = positions):

- `probs.csv`        12 x 101, columns are 1-based positions 26..126 (window centres)
- `p_values.csv`     12 x 101, same columns
- `pred_labels.csv`  12 x 151, columns 0..150 (0-based index over the full sequence), 1 = p < alpha
- `attention.csv`    12 x 151, top-3 attention windows (w=3) for significant sites, 0/1 mask
- `visualization.json` upstream text report

Two independent runs differ by <= 2.3e-7 in `probs` (deterministic).
Upstream reports 22 significant sites at alpha=0.05 for this sequence.
