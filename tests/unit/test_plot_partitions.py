"""Verify solution/CSV joins and closest-to-zero rankings without plotting dependencies."""

from contextlib import redirect_stdout
import csv
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import plot_partitions as plotter


class PlotPartitionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.solutions = self.root / "solutions"
        self.solutions.mkdir()
        self.csv_path = self.root / "ppa.csv"

    def write_solution(self, idx, cut_size, imbalance):
        folder = self.solutions / f"s{idx:04d}"
        folder.mkdir()
        (folder / "solution.json").write_text(json.dumps({
            "solution_id": folder.name, "source_index": idx,
            "cost": [cut_size, imbalance],
        }))

    def write_csv(self, rows):
        with self.csv_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["idx", "wns", "cost"])
            writer.writerows(rows)

    def test_join_uses_original_index_not_csv_or_folder_order(self):
        self.write_solution(10, 1500, 250000)
        self.write_solution(2, 1200, 500000)
        self.write_csv([(10, -0.75, 0.4), (2, -0.55, 0.2)])
        solutions = plotter.load_solutions(self.solutions, self.csv_path)
        self.assertEqual(solutions, [
            plotter.Solution(2, 1200, 500000, -0.55, 0.2),
            plotter.Solution(10, 1500, 250000, -0.75, 0.4),
        ])

    def test_rank_is_by_wns_closest_to_zero_and_labels_only_five(self):
        solutions = [plotter.Solution(idx, 100, 200, -0.1 * idx, float(10 - idx))
                     for idx in range(8)]
        ranked = plotter.top_solutions(solutions)
        self.assertEqual([solution.idx for solution in ranked], [0, 1, 2, 3, 4])
        self.assertEqual(plotter.top_solutions(solutions, 0), [])

    def test_rank_compares_distance_to_zero_for_both_signs(self):
        solutions = [plotter.Solution(idx, 100, 200, wns, None)
                     for idx, wns in enumerate((-0.3, 0.1, -0.05, 0.04))]
        self.assertEqual([point.idx for point in plotter.top_solutions(solutions)], [3, 2, 1, 0])

    def test_tied_wns_uses_original_index(self):
        solutions = [plotter.Solution(idx, 100, 200, -0.5, float(10 - idx))
                     for idx in (10, 2, 5)]
        self.assertEqual([point.idx for point in plotter.top_solutions(solutions)], [2, 5, 10])

    def test_balanced_min_cut_minimizes_imbalance_before_cut_size(self):
        solutions = [
            plotter.Solution(0, 0, 1000, -0.1, None),
            plotter.Solution(1, 200, 100, -0.5, None),
            plotter.Solution(2, 100, 200, -0.2, None),
        ]
        self.assertEqual(plotter.balanced_min_cut(solutions).idx, 1)

    def test_balanced_min_cut_breaks_imbalance_ties_by_cut_then_index(self):
        solutions = [
            plotter.Solution(19, 1806, 272491.6, -0.48, None),
            plotter.Solution(20, 1471, 272491.6, -0.6, None),
            plotter.Solution(21, 1471, 272491.6, -0.4, None),
        ]
        self.assertEqual(plotter.balanced_min_cut(solutions).idx, 20)

    def test_balanced_min_cut_selection_is_independent_of_metric_availability(self):
        solutions = [
            plotter.Solution(0, 100, 200, -0.5, None),
            plotter.Solution(1, 200, 100, None, None),
        ]
        self.assertEqual(plotter.balanced_min_cut(solutions).idx, 1)
        self.assertIsNone(plotter.balanced_min_cut([]))

    def test_missing_csv_or_wns_keeps_geometry_but_cannot_rank(self):
        for idx in range(4):
            self.write_solution(idx, idx, 1000 + idx)
        self.write_csv([(0, "", ""), (1, -0.5, ""), (2, 0, 0)])
        solutions = plotter.load_solutions(self.solutions, self.csv_path)
        self.assertEqual(len(solutions), 4)
        self.assertIsNone(solutions[0].value)
        self.assertIsNone(solutions[3].value)
        self.assertEqual([point.idx for point in plotter.top_solutions(solutions)], [2, 1])

    def test_numeric_and_s_prefixed_csv_indices_are_accepted(self):
        self.write_solution(48, 1534, 579091.6)
        self.write_csv([("s0048", -0.55, 0.3)])
        solutions = plotter.load_solutions(self.solutions, self.csv_path)
        self.assertEqual(solutions[0].idx, 48)
        self.assertEqual(solutions[0].value, -0.55)

    def test_tns_uses_tns_for_colors_ranks_and_missing_values(self):
        for idx in range(3):
            self.write_solution(idx, 100 + idx, 1000 + idx)
        self.csv_path.write_text("idx,wns,tns,cost\n0,-0.1,-100,0.1\n1,-0.8,-1,0.2\n2,-0.01,,0.3\n")
        solutions = plotter.load_solutions(self.solutions, self.csv_path, metric="tns")
        self.assertEqual([point.value for point in solutions], [-100, -1, None])
        self.assertEqual([point.idx for point in plotter.top_solutions(solutions)], [1, 0])

    def test_tns_mode_does_not_require_wns_or_cost_columns(self):
        self.csv_path.write_text("idx,tns\n0,-2\n")
        self.assertEqual(plotter.read_metrics(self.csv_path, metric="tns"), {0: (-2, None)})

    def test_tns_mode_rejects_missing_tns_column(self):
        self.write_csv([(0, -0.5, 0.1)])
        with self.assertRaisesRegex(ValueError, "expected idx and tns"):
            plotter.read_metrics(self.csv_path, metric="tns")

    def test_rwl_and_power_read_their_columns_and_rank_lower_values_first(self):
        for idx in range(4):
            self.write_solution(idx, 100 + idx, 1000 + idx)
        self.csv_path.write_text(
            "idx,wns,after_routing_wirelength,power\n"
            "0,-0.05,3000000,0.15\n1,-0.9,2000000,0.2\n"
            "2,-0.1,,0.1\n3,,0,\n"
        )
        cases = (
            ("rwl", [3000000, 2000000, None, 0], [3, 1, 0]),
            ("power", [0.15, 0.2, 0.1, None], [2, 0, 1]),
        )
        for metric, expected_values, expected_rank in cases:
            with self.subTest(metric=metric):
                solutions = plotter.load_solutions(self.solutions, self.csv_path, metric=metric)
                self.assertEqual([point.value for point in solutions], expected_values)
                self.assertEqual([point.idx for point in plotter.top_solutions(solutions)], expected_rank)

    def test_rwl_and_power_reject_missing_metric_columns(self):
        self.write_csv([(0, -0.5, 0.1)])
        for metric, column in (("rwl", "after_routing_wirelength"), ("power", "power")):
            with self.subTest(metric=metric), self.assertRaisesRegex(ValueError, f"expected idx and {column}"):
                plotter.read_metrics(self.csv_path, metric=metric)

    def test_duplicate_csv_indices_are_rejected(self):
        self.write_csv([(2, -0.5, 0.1), ("s0002", -0.6, 0.2)])
        with self.assertRaisesRegex(ValueError, "duplicate partition index 2"):
            plotter.read_metrics(self.csv_path)

    def test_cost_column_is_optional_for_wns_ranking(self):
        self.csv_path.write_text("idx,wns\n0,-0.5\n")
        self.assertEqual(plotter.read_metrics(self.csv_path), {0: (-0.5, None)})

    def test_missing_wns_column_is_rejected(self):
        self.csv_path.write_text("idx,cost\n0,0.1\n")
        with self.assertRaisesRegex(ValueError, "expected idx and wns"):
            plotter.read_metrics(self.csv_path)

    def test_invalid_wns_cannot_be_used_as_a_color_value(self):
        for invalid in ("NaN", "inf", "broken"):
            with self.subTest(value=invalid):
                self.write_csv([(0, invalid, 0.1)])
                with self.assertRaisesRegex(ValueError, "expected a finite number"):
                    plotter.read_metrics(self.csv_path)

    def test_invalid_geometry_is_rejected(self):
        self.write_solution(0, -1, 1000)
        self.write_csv([(0, -0.5, 0.1)])
        with self.assertRaisesRegex(ValueError, "must be nonnegative"):
            plotter.load_solutions(self.solutions, self.csv_path)


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Matplotlib is needed for rendering checks")
class PlotRenderingTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.plt = plt
        self.addCleanup(plt.close, "all")

    def sample_data(self, constant=False):
        values = {"wns": (-1.0, -0.5), "tns": (-100.0, -1.0),
                  "rwl": (3000000.0, 2000000.0), "power": (0.2, 0.1)}
        return {
            metric: [plotter.Solution(0, 100, 300, pair[0], None),
                     plotter.Solution(1, 200, 100, pair[0] if constant else pair[1], None),
                     plotter.Solution(2, 300, 200, None, None)]
            for metric, pair in values.items()
        }

    def test_merge_has_four_panels_outer_titles_and_no_colorbars(self):
        fig = plotter.make_merged_plot(self.sample_data(), top_n=2)
        fig.canvas.draw()
        self.assertEqual(len(fig.axes), 4)
        self.assertEqual({text.get_text() for text in fig.texts}, {"Cut Size", "Area Imbalance"})
        for ax, metric in zip(fig.axes, plotter.MERGE_METRICS):
            self.assertEqual(ax.get_xlabel(), "")
            self.assertEqual(ax.get_ylabel(), "")
            labels = [text.get_text() for text in ax.get_legend().get_texts()]
            self.assertEqual(labels, ["Balanced Min-Cut", f"Top 2 by {plotter.METRICS[metric].label}"])
        self.assertTrue(fig.axes[0].get_shared_x_axes().joined(fig.axes[0], fig.axes[3]))
        self.assertTrue(fig.axes[0].get_shared_y_axes().joined(fig.axes[0], fig.axes[3]))
        self.assertFalse(any(text.get_visible() for text in fig.axes[0].get_xticklabels()))
        self.assertFalse(any(text.get_visible() for text in fig.axes[1].get_yticklabels()))

    def test_merge_preserves_individual_plot_colors_including_constant_metrics(self):
        for constant in (False, True):
            data = self.sample_data(constant)
            merged = plotter.make_merged_plot(data, top_n=2)
            merged.canvas.draw()
            for merged_ax, metric in zip(merged.axes, plotter.MERGE_METRICS):
                with self.subTest(metric=metric, constant=constant):
                    single = plotter.make_plot(data[metric], top_n=2, metric=metric)
                    single.canvas.draw()
                    single_colors = [collection.get_facecolors().tolist()
                                     for collection in single.axes[0].collections]
                    merged_colors = [collection.get_facecolors().tolist()
                                     for collection in merged_ax.collections]
                    self.assertEqual(single_colors, merged_colors)
                    self.plt.close(single)
            self.plt.close(merged)

    def test_merge_cli_writes_one_default_png(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            solutions = root / "solutions"
            for idx in range(2):
                folder = solutions / f"s{idx:04d}"
                folder.mkdir(parents=True)
                (folder / "solution.json").write_text(json.dumps({"cost": [100 + idx, 300 - idx]}))
            csv_path = root / "ppa.csv"
            csv_path.write_text("idx,wns,tns,after_routing_wirelength,power\n"
                                "0,-1,-100,3000000,0.2\n1,-0.5,-1,2000000,0.1\n")
            with redirect_stdout(io.StringIO()):
                result = plotter.main([str(solutions), str(csv_path), "--metric", "merge"])
            self.assertEqual(result, 0)
            self.assertEqual([path.name for path in root.glob("*.png")], ["partition_merge.png"])
            self.assertTrue((root / "partition_merge.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
