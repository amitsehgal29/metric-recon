#!/usr/bin/env python3
"""metric-recon: reconcile aggregated measures between two CSVs, per key.

Standard library only. Groups source and target fact rows by a key, aggregates
one or more numeric columns (sum, avg, min, max or count), and reports which
keys match, which drifted beyond a tolerance, and which are missing on either
side. Exits 1 when anything fails, so it can gate a pipeline step.
"""
import argparse
import csv
import json
import sys


AGGS = ("sum", "avg", "min", "max", "count")


def parse_columns(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_number(raw):
    """Return (number, kind) where kind is 'ok', 'empty' or 'unparseable'."""
    text = (raw or "").strip().replace(",", "")
    if text.startswith("$"):
        text = text[1:].strip()
    if text == "":
        return None, "empty"
    try:
        return float(text), "ok"
    except ValueError:
        return None, "unparseable"


def key_of(row, keys):
    return tuple((row.get(name) or "").strip() for name in keys)


def key_label(keys, key):
    if len(keys) == 1:
        return key[0]
    return dict(zip(keys, key))


def aggregate(rows, keys, metrics, agg):
    """Group rows and aggregate metrics.

    Returns (values, unparseable) where values is
    {key: {metric: number_or_None}} and unparseable is
    {metric: count of bad non-empty cells}.
    """
    buckets = {}
    unparseable = {metric: 0 for metric in metrics}
    for row in rows:
        k = key_of(row, keys)
        bucket = buckets.setdefault(k, {metric: [] for metric in metrics})
        for metric in metrics:
            number, kind = parse_number(row.get(metric))
            if kind == "ok":
                bucket[metric].append(number)
            elif kind == "unparseable":
                unparseable[metric] += 1

    values = {}
    for k, bucket in buckets.items():
        values[k] = {}
        for metric in metrics:
            nums = bucket[metric]
            if agg == "count":
                values[k][metric] = float(len(nums))
                continue
            if not nums:
                values[k][metric] = None
                continue
            if agg == "sum":
                values[k][metric] = sum(nums)
            elif agg == "avg":
                values[k][metric] = sum(nums) / len(nums)
            elif agg == "min":
                values[k][metric] = min(nums)
            elif agg == "max":
                values[k][metric] = max(nums)
    return values, unparseable


def clean_number(value):
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, float):
        return round(value, 6)
    return value


def compare_one(source, target, tolerance_pct, tolerance_abs):
    if source is None and target is None:
        return {
            "source": None,
            "target": None,
            "diff": None,
            "diff_pct": None,
            "status": "no_values",
        }
    if source is None:
        return {
            "source": None,
            "target": clean_number(target),
            "diff": None,
            "diff_pct": None,
            "status": "missing_in_source",
        }
    if target is None:
        return {
            "source": clean_number(source),
            "target": None,
            "diff": None,
            "diff_pct": None,
            "status": "missing_in_target",
        }
    diff = target - source
    if source == 0:
        pct = 0.0 if diff == 0 else float("inf")
        ok = diff == 0 or abs(diff) <= tolerance_abs
    else:
        pct = abs(diff) / abs(source) * 100
        ok = abs(diff) <= tolerance_abs or pct <= tolerance_pct
    if diff == 0:
        status = "match"
    elif ok:
        status = "within_tolerance"
    else:
        status = "mismatch"
    return {
        "source": clean_number(source),
        "target": clean_number(target),
        "diff": clean_number(diff),
        "diff_pct": None if pct == float("inf") else round(pct, 4),
        "status": status,
    }


FAIL_STATUSES = {
    "mismatch",
    "missing_in_source",
    "missing_in_target",
    "no_values",
}


def reconcile(source_values, target_values, metrics, tolerance_pct=0.0, tolerance_abs=0.0):
    results = []
    all_keys = sorted(set(source_values) | set(target_values))
    for key in all_keys:
        src_row = source_values.get(key, {})
        tgt_row = target_values.get(key, {})
        if key not in source_values:
            for metric in metrics:
                item = compare_one(None, tgt_row.get(metric), tolerance_pct, tolerance_abs)
                item["key"] = key
                item["metric"] = metric
                results.append(item)
            continue
        if key not in target_values:
            for metric in metrics:
                item = compare_one(src_row.get(metric), None, tolerance_pct, tolerance_abs)
                item["key"] = key
                item["metric"] = metric
                results.append(item)
            continue
        for metric in metrics:
            item = compare_one(
                src_row.get(metric), tgt_row.get(metric), tolerance_pct, tolerance_abs
            )
            item["key"] = key
            item["metric"] = metric
            results.append(item)

    failed = [row for row in results if row["status"] in FAIL_STATUSES]
    totals = {}
    for metric in metrics:
        src_total = sum(
            row[metric] for row in source_values.values() if row.get(metric) is not None
        )
        tgt_total = sum(
            row[metric] for row in target_values.values() if row.get(metric) is not None
        )
        totals[metric] = {
            "source": clean_number(src_total),
            "target": clean_number(tgt_total),
        }
    return {
        "keys": len(all_keys),
        "metrics": metrics,
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "totals": totals,
        "ok": not failed,
        "results": results,
    }


def missing_columns(fieldnames, required):
    return [name for name in required if name not in (fieldnames or [])]


def load_rows(path, required):
    with open(path, newline="", encoding="utf-8") as handle:
        fieldnames, rows = (lambda r: (list(r.fieldnames or []), list(r)))(
            csv.DictReader(handle)
        )
    missing = missing_columns(fieldnames, required)
    if missing:
        raise ValueError(f"{path}: missing column(s): {', '.join(missing)}")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconcile aggregated numeric measures between source and target CSVs."
    )
    parser.add_argument("source", help="source CSV (fact or already aggregated)")
    parser.add_argument("target", help="target CSV (fact or already aggregated)")
    parser.add_argument(
        "-k",
        "--key",
        required=True,
        help="key column(s), comma-separated, e.g. order_date or order_date,region",
    )
    parser.add_argument(
        "-m",
        "--metrics",
        required=True,
        help="numeric measure column(s) to aggregate, comma-separated",
    )
    parser.add_argument(
        "--agg",
        choices=AGGS,
        default="sum",
        help="aggregation to apply before comparing (default: sum)",
    )
    parser.add_argument(
        "--tolerance-pct",
        type=float,
        default=0.0,
        help="allowed drift as a percent of the source value",
    )
    parser.add_argument(
        "--tolerance-abs",
        type=float,
        default=0.0,
        help="allowed absolute drift. A key passes if either tolerance holds",
    )
    parser.add_argument(
        "--failures-only",
        action="store_true",
        help="list only failing key/metric rows",
    )
    parser.add_argument(
        "--max-unparseable",
        type=int,
        default=0,
        help="allowed unparseable numeric cells before exit 1 (default: 0)",
    )
    args = parser.parse_args(argv)

    keys = parse_columns(args.key)
    metrics = parse_columns(args.metrics)
    if not keys or not metrics:
        print("metric-recon: --key and --metrics are required", file=sys.stderr)
        return 2

    try:
        source_rows = load_rows(args.source, keys + metrics)
        target_rows = load_rows(args.target, keys + metrics)
    except (OSError, ValueError) as exc:
        print(f"metric-recon: {exc}", file=sys.stderr)
        return 2

    source_values, source_bad = aggregate(source_rows, keys, metrics, args.agg)
    target_values, target_bad = aggregate(target_rows, keys, metrics, args.agg)
    report = reconcile(
        source_values,
        target_values,
        metrics,
        tolerance_pct=args.tolerance_pct,
        tolerance_abs=args.tolerance_abs,
    )
    report["agg"] = args.agg
    report["keys_columns"] = keys
    report["unparseable"] = {"source": source_bad, "target": target_bad}

    # Replace tuple keys with a friendly label for JSON.
    for row in report["results"]:
        row["key"] = key_label(keys, row["key"])

    unparseable_total = sum(source_bad.values()) + sum(target_bad.values())
    reasons = []
    if unparseable_total > args.max_unparseable:
        reasons.append(
            f"unparseable {unparseable_total} > max-unparseable {args.max_unparseable}"
        )
    if not report["ok"]:
        reasons.append(f"{report['failed']} metric comparison(s) failed")

    if args.failures_only:
        report["results"] = [
            row for row in report["results"] if row["status"] in FAIL_STATUSES
        ]

    report["ok"] = not reasons
    if reasons:
        report["failed_checks"] = reasons

    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
