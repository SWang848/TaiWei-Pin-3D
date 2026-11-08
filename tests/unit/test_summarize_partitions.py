"""Check partition identity, final metric selection, and incomplete-run handling."""

from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import summarize_partitions as summary


class SummarizePartitionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "run"
        self.results = self.run / "results"
        self.results.mkdir(parents=True)

    def write(self, partition, relative, text):
        path = self.results / partition / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def evaluate(self, input_dir=None):
        output = self.root / "out" / "ppa.csv"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            result = summary.main([str(input_dir or self.run), "-o", str(output)])
        self.assertEqual(result, 0)
        with output.open(newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, list(summary.COLUMNS))
            return list(reader), errors.getvalue()

    def test_cli_sorts_original_indices_and_prefers_eval_json(self):
        for name in ("s0010", "s0002"):
            self.write(name, "openroad_eval.json", json.dumps({
                "finish__timing__setup__tns": {"value": -12.5},
                "finish__timing__setup__ws": {"value": -0.25},
                "finish__power__total": {"value": 0.156},
                "_meta": {"summary_path": "/unavailable/container/final_summary.txt"},
            }))
            self.write(name, "reports/tech/design/variant/openroad_eval.json", "{}")
            self.write(name, "logs/tech/design/variant/final_summary.txt",
                       "Wire Length       123456.78\nTNS -999\nWNS -9\nTotal Power 9\n")
        result = subprocess.run(
            [sys.executable, str(REPO / "summarize_partitions.py"), str(self.run)],
            cwd=self.root, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with (self.run / "partition_ppa.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["idx"] for row in rows], ["2", "10"])
        self.assertEqual(rows[0], {
            "idx": "2", "after_routing_wirelength": "123456.78",
            "tns": "-12.5", "wns": "-0.25", "power": "0.156", "cost": "0.0",
        })

    def test_summary_alone_is_sufficient_and_preserves_zero(self):
        self.write("s0000", "logs/final_summary.txt",
                   "Wire Length 500.5\nTNS 0\nWNS 0\nTotal Power 1.2e-02\n")
        rows, errors = self.evaluate(self.results)
        self.assertEqual(rows[0]["tns"], "0.0")
        self.assertEqual(rows[0]["wns"], "0.0")
        self.assertEqual(rows[0]["power"], "0.012")
        self.assertEqual(errors, "")

    def test_finish_and_per_net_reports_supply_missing_metrics(self):
        self.write("s0048", "reports/variant/6_finish.rpt",
                   "tns max -6.5\nwns max -0.55\n"
                   "Sequential 1e-2 2e-2 1e-3 3.1e-2 20.0%\n"
                   "Total 8.82e-02 5.88e-02 9.49e-03 1.56e-01 100.0%\n")
        self.write("s0048", "reports/variant/6_wire_length.rpt",
                   "tool net total_wl #pins\ndrt: net_a 1.25 2\ndrt: net_b 2.75 3\n")
        rows, errors = self.evaluate(self.results / "s0048")
        self.assertEqual(rows[0], {
            "idx": "48", "after_routing_wirelength": "4.0",
            "tns": "-6.5", "wns": "-0.55", "power": "0.156", "cost": "0.0",
        })
        self.assertEqual(errors, "")

    def test_failed_partition_does_not_use_partial_route_or_placement_values(self):
        self.write("s0014", "run.log", "tns max -99\nwns max -2\nHPWL 42\n")
        self.write("s0014", "logs/5_2_route.log", "Total wire length = 900 um.\n")
        self.write("s0014", "reports/6_wire_length.rpt", "tool net total_wl #pins\n")
        rows, errors = self.evaluate()
        self.assertEqual(rows, [{"idx": "14", **dict.fromkeys(summary.COLUMNS[1:], "")}])
        self.assertIn("s0014: missing final metrics", errors)

    def test_missing_summary_wirelength_is_summed_without_overriding_other_values(self):
        self.write("s0003", "logs/final_summary.txt",
                   "Wire Length N/A\nTNS -10\nWNS -0.2\nTotal Power 0.1\n")
        self.write("s0003", "reports/6_wire_length.rpt", "drt: net_a 1.234e2 2\n")
        rows, errors = self.evaluate()
        self.assertEqual(rows[0]["after_routing_wirelength"], "123.4")
        self.assertEqual(rows[0]["tns"], "-10.0")
        self.assertEqual(errors, "")

    def test_invalid_json_falls_back_to_final_summary(self):
        self.write("s0001", "openroad_eval.json", "{broken")
        self.write("s0001", "logs/final_summary.txt",
                   "Wire Length 5\nTNS 0\nWNS 0\nTotal Power 0\n")
        rows, errors = self.evaluate()
        self.assertEqual(rows[0]["power"], "0.0")
        self.assertIn("invalid evaluation JSON", errors)

    def test_wirelength_rounding_matches_flow_summary_accumulation(self):
        text = "drt: net_a 0.1 2\ndrt: net_b 0.2 3\ndrt: net_c 0.045 2\n"
        self.assertEqual(summary.routed_wirelength(text), 0.35)

    def test_nonfinite_and_boolean_metrics_are_missing(self):
        self.write("s0001", "openroad_eval.json", json.dumps({
            "finish__timing__setup__tns": {"value": True},
            "finish__timing__setup__ws": {"value": "inf"},
            "finish__power__total": {"value": "NaN"},
        }))
        rows, errors = self.evaluate()
        self.assertEqual(rows[0]["tns"], "")
        self.assertEqual(rows[0]["wns"], "")
        self.assertEqual(rows[0]["power"], "")
        self.assertIn("missing final metrics", errors)

    def test_multiple_variants_are_rejected_before_writing_csv(self):
        for variant in ("a", "b"):
            self.write("s0000", f"logs/{variant}/final_summary.txt", "TNS 0\n")
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as raised:
            summary.main([str(self.run)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("multiple final_summary.txt files", errors.getvalue())
        self.assertFalse((self.run / "partition_ppa.csv").exists())

    def test_empty_input_does_not_create_a_header_only_csv(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            summary.main([str(self.run)])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse((self.run / "partition_ppa.csv").exists())

    def test_cost_normalizes_slack_toward_higher_values_and_uses_l2(self):
        measurements = [
            (100, 0, 0, 0.1),      # Best in every metric: normalized vector is all 0.
            (200, -10, -2, 0.2),   # Worst in every metric: normalized vector is all 1.
            (150, -5, -1, 0.15),   # Midpoint in every metric: L2 norm is 1.
        ]
        for index, (wire, tns, wns, power) in enumerate(measurements):
            self.write(f"s{index:04d}", "logs/final_summary.txt",
                       f"Wire Length {wire}\nTNS {tns}\nWNS {wns}\nTotal Power {power}\n")
        rows, errors = self.evaluate()
        for row, expected in zip(rows, (0, 2, 1)):
            self.assertAlmostEqual(float(row["cost"]), expected)
        self.assertEqual(errors, "")

    def test_cost_combines_independent_tradeoffs(self):
        measurements = [
            (100, -10, -2, 0.1),
            (200, 0, 0, 0.2),
            (125, -2.5, -1, 0.125),  # Components: 0.25, 0.25, 0.5, 0.25.
        ]
        for index, (wire, tns, wns, power) in enumerate(measurements):
            self.write(f"s{index:04d}", "logs/final_summary.txt",
                       f"Wire Length {wire}\nTNS {tns}\nWNS {wns}\nTotal Power {power}\n")
        rows, _ = self.evaluate()
        self.assertAlmostEqual(float(rows[0]["cost"]), 2 ** 0.5)
        self.assertAlmostEqual(float(rows[1]["cost"]), 2 ** 0.5)
        self.assertAlmostEqual(float(rows[2]["cost"]), 0.4375 ** 0.5)

    def test_constant_metrics_contribute_zero_to_cost(self):
        for index, wire in enumerate((100, 200)):
            self.write(f"s{index:04d}", "logs/final_summary.txt",
                       f"Wire Length {wire}\nTNS 0\nWNS 0\nTotal Power 0.1\n")
        rows, errors = self.evaluate()
        self.assertEqual([float(row["cost"]) for row in rows], [0, 1])
        self.assertEqual(errors, "")

    def test_incomplete_partition_has_no_cost_and_does_not_change_bounds(self):
        self.write("s0000", "logs/final_summary.txt",
                   "Wire Length 100\nTNS 0\nWNS 0\nTotal Power 0.1\n")
        self.write("s0001", "logs/final_summary.txt",
                   "Wire Length 200\nTNS -10\nWNS -2\nTotal Power 0.2\n")
        self.write("s0002", "logs/final_summary.txt",
                   "Wire Length 10000\nTNS -1000000\nWNS -1000000\nTotal Power N/A\n")
        rows, errors = self.evaluate()
        self.assertEqual([row["cost"] for row in rows], ["0.0", "2.0", ""])
        self.assertEqual(rows[2]["after_routing_wirelength"], "10000.0")
        self.assertIn("s0002: missing final metrics: power", errors)


if __name__ == "__main__":
    unittest.main()
