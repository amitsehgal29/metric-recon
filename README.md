# metric-recon

Reconcile numeric measures between a source CSV and a target CSV, key by key. It groups the rows, aggregates the measure columns (sum by default, or avg, min, max, count), and reports `match`, `within_tolerance`, `mismatch`, `missing_in_target`, or `missing_in_source`. Python 3 standard library only.

Use it after a load to check that revenue, quantity, or any other measure landed in the warehouse the same way the source sent it. It complements `count-recon`, which only compares pre-aggregated row counts.

## Usage

```bash
python3 metric_recon.py samples/source_orders.csv samples/target_orders.csv \
  --key order_date --metrics amount,qty --failures-only
```

Options:

- `--key order_date,region` uses a composite key.
- `--metrics amount,qty` lists the numeric columns to aggregate and compare.
- `--agg sum` (default), or `avg`, `min`, `max`, `count`.
- `--tolerance-pct 0.5` allows drift as a percent of the source value.
- `--tolerance-abs 10` allows absolute drift. A comparison passes if either tolerance holds.
- `--failures-only` lists only failing key/metric rows.
- `--max-unparseable 0` (default) is how many non-numeric cells are allowed before exit 1.
- Values like `1,200.50` and `$80.00` are accepted. Empty cells are skipped. A source value of 0 with a non-zero target always fails.

Exit codes: `0` all comparisons passed, `1` a comparison failed or too many unparseable values, `2` bad input.

## Example output (real run on the samples, with `--failures-only`)

```json
{
  "keys": 4,
  "metrics": ["amount", "qty"],
  "passed": 3,
  "failed": 5,
  "totals": {
    "amount": {"source": 430, "target": 360},
    "qty": {"source": 8, "target": 8}
  },
  "ok": false,
  "results": [
    {"source": 200, "target": 190, "diff": -10, "diff_pct": 5.0, "status": "mismatch",
     "key": "2026-11-12", "metric": "amount"},
    {"source": 80, "target": null, "diff": null, "diff_pct": null, "status": "missing_in_target",
     "key": "2026-11-13", "metric": "amount"},
    {"source": 1, "target": null, "diff": null, "diff_pct": null, "status": "missing_in_target",
     "key": "2026-11-13", "metric": "qty"},
    {"source": null, "target": 20, "diff": null, "diff_pct": null, "status": "missing_in_source",
     "key": "2026-11-14", "metric": "amount"},
    {"source": null, "target": 1, "diff": null, "diff_pct": null, "status": "missing_in_source",
     "key": "2026-11-14", "metric": "qty"}
  ],
  "agg": "sum",
  "keys_columns": ["order_date"],
  "unparseable": {"source": {"amount": 0, "qty": 0}, "target": {"amount": 0, "qty": 0}},
  "failed_checks": ["5 metric comparison(s) failed"]
}
```

(Reformatted for width; the tool prints the same JSON with one field per line. Exit code was 1.)

11 November matches on both amount (150) and qty (3). 12 November is short 10 on amount (5%) but qty still matches. 13 November never landed in the target, and 14 November appears only in the target.

## Tests

```bash
python3 -m unittest -v
```
