#!/usr/bin/env python3
import io
import json
import os
import tempfile
import unittest

import metric_recon as mr


SAMPLES = os.path.join(os.path.dirname(__file__), "samples")
SOURCE = os.path.join(SAMPLES, "source_orders.csv")
TARGET = os.path.join(SAMPLES, "target_orders.csv")


def run_main(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    old_out, old_err = mr.sys.stdout, mr.sys.stderr
    mr.sys.stdout, mr.sys.stderr = stdout, stderr
    try:
        code = mr.main(argv)
    finally:
        mr.sys.stdout, mr.sys.stderr = old_out, old_err
    out = stdout.getvalue()
    payload = json.loads(out) if out.strip().startswith("{") else None
    return code, payload, stderr.getvalue()


class ParseAndAggTests(unittest.TestCase):
    def test_parse_number(self):
        self.assertEqual(mr.parse_number("1,200.50"), (1200.50, "ok"))
        self.assertEqual(mr.parse_number("$80.00"), (80.0, "ok"))
        self.assertEqual(mr.parse_number(""), (None, "empty"))
        self.assertEqual(mr.parse_number("  "), (None, "empty"))
        self.assertEqual(mr.parse_number("n/a"), (None, "unparseable"))

    def test_aggregate_sum(self):
        rows = [
            {"d": "11", "amount": "100.00", "qty": "2"},
            {"d": "11", "amount": "50.00", "qty": "1"},
            {"d": "12", "amount": "200.00", "qty": "4"},
        ]
        values, bad = mr.aggregate(rows, ["d"], ["amount", "qty"], "sum")
        self.assertEqual(values[("11",)]["amount"], 150.0)
        self.assertEqual(values[("11",)]["qty"], 3.0)
        self.assertEqual(values[("12",)]["amount"], 200.0)
        self.assertEqual(bad, {"amount": 0, "qty": 0})

    def test_aggregate_avg_and_count(self):
        rows = [
            {"d": "11", "amount": "10"},
            {"d": "11", "amount": "30"},
        ]
        avgs, _ = mr.aggregate(rows, ["d"], ["amount"], "avg")
        self.assertEqual(avgs[("11",)]["amount"], 20.0)
        counts, _ = mr.aggregate(rows, ["d"], ["amount"], "count")
        self.assertEqual(counts[("11",)]["amount"], 2.0)


class ReconcileTests(unittest.TestCase):
    def test_sample_facts(self):
        source_rows = mr.load_rows(SOURCE, ["order_date", "amount", "qty"])
        target_rows = mr.load_rows(TARGET, ["order_date", "amount", "qty"])
        src, _ = mr.aggregate(source_rows, ["order_date"], ["amount", "qty"], "sum")
        tgt, _ = mr.aggregate(target_rows, ["order_date"], ["amount", "qty"], "sum")
        report = mr.reconcile(src, tgt, ["amount", "qty"])
        self.assertEqual(report["keys"], 4)
        self.assertFalse(report["ok"])
        by = {(row["key"], row["metric"]): row for row in report["results"]}
        self.assertEqual(by[(("2026-11-11",), "amount")]["status"], "match")
        self.assertEqual(by[(("2026-11-11",), "amount")]["source"], 150)
        self.assertEqual(by[(("2026-11-12",), "amount")]["status"], "mismatch")
        self.assertEqual(by[(("2026-11-12",), "amount")]["source"], 200)
        self.assertEqual(by[(("2026-11-12",), "amount")]["target"], 190)
        self.assertEqual(by[(("2026-11-12",), "qty")]["status"], "match")
        self.assertEqual(by[(("2026-11-13",), "amount")]["status"], "missing_in_target")
        self.assertEqual(by[(("2026-11-14",), "amount")]["status"], "missing_in_source")

    def test_tolerance(self):
        src = {("a",): {"amount": 200.0}}
        tgt = {("a",): {"amount": 190.0}}
        tight = mr.reconcile(src, tgt, ["amount"], tolerance_pct=0.0, tolerance_abs=0)
        self.assertEqual(tight["results"][0]["status"], "mismatch")
        loose_pct = mr.reconcile(src, tgt, ["amount"], tolerance_pct=5.0, tolerance_abs=0)
        self.assertEqual(loose_pct["results"][0]["status"], "within_tolerance")
        loose_abs = mr.reconcile(src, tgt, ["amount"], tolerance_pct=0.0, tolerance_abs=10)
        self.assertEqual(loose_abs["results"][0]["status"], "within_tolerance")

    def test_zero_source_with_target_fails(self):
        src = {("a",): {"amount": 0.0}}
        tgt = {("a",): {"amount": 5.0}}
        report = mr.reconcile(src, tgt, ["amount"])
        self.assertEqual(report["results"][0]["status"], "mismatch")
        self.assertIsNone(report["results"][0]["diff_pct"])


class CliTests(unittest.TestCase):
    def test_sample_run_fails(self):
        code, payload, _err = run_main(
            [SOURCE, TARGET, "--key", "order_date", "--metrics", "amount,qty"]
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["agg"], "sum")
        self.assertEqual(payload["keys"], 4)
        # 11-11 amount+qty match, 11-12 amount mismatch and qty match,
        # 11-13 amount+qty missing_in_target, 11-14 amount+qty missing_in_source
        self.assertEqual(payload["failed"], 5)
        self.assertEqual(payload["passed"], 3)
        self.assertEqual(payload["totals"]["amount"]["source"], 430)
        self.assertEqual(payload["totals"]["amount"]["target"], 360)

    def test_failures_only_and_tolerance(self):
        code, payload, _err = run_main(
            [
                SOURCE,
                TARGET,
                "--key",
                "order_date",
                "--metrics",
                "amount",
                "--tolerance-abs",
                "10",
                "--failures-only",
            ]
        )
        self.assertEqual(code, 1)
        # 11-12 is within 10 of 200, so failures-only drops it along with the match
        keys = [row["key"] for row in payload["results"]]
        self.assertEqual(keys, ["2026-11-13", "2026-11-14"])
        self.assertNotIn("2026-11-12", keys)
        self.assertNotIn("2026-11-11", keys)

    def test_composite_key(self):
        code, payload, _err = run_main(
            [SOURCE, TARGET, "--key", "order_date,region", "--metrics", "amount"]
        )
        self.assertEqual(code, 1)
        matches = [
            row for row in payload["results"]
            if row["status"] == "match"
        ]
        self.assertTrue(any(row["key"] == {"order_date": "2026-11-11", "region": "DXB"} for row in matches))

    def test_missing_column_exits_two(self):
        code, payload, err = run_main(
            [SOURCE, TARGET, "--key", "order_date", "--metrics", "nope"]
        )
        self.assertEqual(code, 2)
        self.assertIsNone(payload)
        self.assertIn("missing column", err)

    def test_unparseable_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.csv")
            tgt = os.path.join(tmp, "tgt.csv")
            with open(src, "w", encoding="utf-8") as handle:
                handle.write("d,amount\n11,100\n11,n/a\n")
            with open(tgt, "w", encoding="utf-8") as handle:
                handle.write("d,amount\n11,100\n")
            code, payload, _err = run_main(
                [src, tgt, "--key", "d", "--metrics", "amount"]
            )
            self.assertEqual(code, 1)
            self.assertEqual(payload["unparseable"]["source"]["amount"], 1)
            code, payload, _err = run_main(
                [src, tgt, "--key", "d", "--metrics", "amount", "--max-unparseable", "1"]
            )
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
