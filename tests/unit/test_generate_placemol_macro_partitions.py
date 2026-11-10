"""Place-MoL graph semantics and TaiWei preparation/export integration."""

from contextlib import redirect_stdout
import csv
import importlib.util
import io
from itertools import combinations, product
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import generate_placemol_macro_partitions as flow
from partition_algorithms.placemol.opendb_adapter import make_problem_instance, read_snapshot

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
HAS_GNN = HAS_NUMPY and all(importlib.util.find_spec(name) is not None
                            for name in ("torch", "torch_geometric", "sklearn"))

SNAPSHOT = """# taiwei-placemol-graph-v1
node\t0\ttop/a\tRAM\tmacro\t10\t20
node\t1\ttop/b\tRAM\tmacro\t5\t10
node\t2\ttop/c\tRAM\tmacro\t10\t10
node\t3\tlogic0\tCORE\tcell\t2\t3
node\t4\t@io:4\t__IO__\tport\t0\t0
net\t5\t0\t0\t1\t3\t4
net\t2\t1\t2
net\t3\t0\t1\t3
net\t2\t0\t2
net\t2\t1\t3
net\t1\t0
net\t0
"""
NETS = [[0, 0, 1, 3, 4], [1, 2], [0, 1, 3], [0, 2], [1, 3], [0], []]


@unittest.skipUnless(HAS_NUMPY, "Place-MoL requires NumPy")
class GraphTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "graph.tsv"
        self.path.write_text(SNAPSHOT)

    def graph(self, **options):
        from partition_algorithms.placemol.graph_builder import GraphBuilder
        return GraphBuilder(make_problem_instance(*read_snapshot(self.path), **options))

    def test_net_deduplication_cell_merging_and_all_partition_metrics(self):
        """Oracle counts net-node pairs, independently of graph adjacency."""
        from partition_algorithms.placemol.graph_cut import GraphCutPartitioner
        graph = self.graph(top_k_ratio=0)
        edges = {}
        for net in NETS:
            for pair in combinations(sorted({min(node, 3) for node in net}), 2):
                edges[pair] = edges.get(pair, 0) + 1
        self.assertEqual(dict(zip(map(tuple, graph.edges), graph.edge_weights)), edges)
        partitioner = GraphCutPartitioner(graph, 0.1)
        for labels in product((False, True), repeat=3):
            bottom = [i for i, flag in enumerate(labels) if flag]
            metrics = partitioner.evaluate_cut(bottom)
            expected_cut = sum(weight for (u, v), weight in edges.items()
                               if v < 3 and labels[u] != labels[v])
            expected_bottom = 6 + sum(area for i, area in enumerate((200, 50, 100)) if labels[i])
            expected_upper = 356 - expected_bottom
            self.assertEqual(metrics["cut_weight"], expected_cut)
            self.assertEqual(metrics["bottom_area"], expected_bottom)
            self.assertEqual(metrics["upper_area"], expected_upper)
            self.assertEqual(metrics["area_diff"], abs(expected_bottom - expected_upper))
            self.assertAlmostEqual(metrics["area_imbalance_ratio"], metrics["area_diff"] / 356)
            self.assertEqual(metrics["is_balanced"], metrics["area_diff"] <= 35.6)

    def test_features_preserve_dimensions_pins_and_distinct_connected_cells(self):
        graph = self.graph(top_k_ratio=0, scale_factor=0.5)
        self.assertEqual(graph.node_features[0].tolist(), [200, 10, 20, 2, 5])
        self.assertEqual(graph.node_features[1].tolist(), [50, 5, 10, 2, 4])
        self.assertEqual(graph.node_features[3][0], 12)
        self.assertAlmostEqual(float(graph.node_features[3][1] * graph.node_features[3][2]), 12, places=5)
        self.assertEqual(graph.node_features[3][3:].tolist(), [2, 4])
        self.assertEqual(graph.get_area_info()["total_area"], 362)

    def test_default_threshold_keeps_fraction_of_edges_and_zero_disables(self):
        self.assertEqual(len(self.graph().edges), 1)  # 5 > .3 * 4^2 -> floor(5 * .3)
        self.assertEqual(len(self.graph(top_k_ratio=0).edges), 5)

    def test_k_hop_shortest_paths_have_fractional_weights(self):
        self.path.write_text(SNAPSHOT[:SNAPSHOT.index("net\t")] + "net\t2\t0\t1\nnet\t2\t1\t2\nnet\t2\t2\t3\n")
        graph = self.graph(top_k_ratio=0, k_hop=3)
        edges = dict(zip(map(tuple, graph.edges), graph.edge_weights))
        self.assertEqual(edges, {(0, 1): 1, (1, 2): 1, (2, 3): 1,
                                 (0, 2): 0.25, (1, 3): 0.25, (0, 3): 0.125})

    def test_edgeless_graph_and_single_macro_min_max(self):
        from partition_algorithms.placemol.graph_cut import GraphCutPartitioner
        self.path.write_text("# taiwei-placemol-graph-v1\nnode\t0\tram\tRAM\tmacro\t10\t20\n")
        graph = self.graph()
        self.assertEqual(graph.edges.shape, (0, 2))
        partitioner = GraphCutPartitioner(graph)
        for method in (partitioner.min_cut, partitioner.max_cut):
            selected = method(seed=42)
            self.assertEqual(selected, [])  # Reference removes one when all macros were selected.
            self.assertFalse(partitioner.evaluate_cut(selected)["is_balanced"])

    def test_malformed_snapshot_is_rejected(self):
        for text in (SNAPSHOT.replace("node\t1", "node\t0"),
                     SNAPSHOT.replace("macro\t10\t20", "macro\tnan\t20"),
                     SNAPSHOT.replace("net\t2\t1\t2", "net\t2\t1\t99"),
                     SNAPSHOT.replace("net\t2\t1\t2", "net\t3\t1\t2")):
            self.path.write_text(text)
            with self.assertRaises(ValueError):
                read_snapshot(self.path)


@unittest.skipUnless(HAS_NUMPY and shutil.which("tclsh"), "NumPy and Tcl are required")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "designs/platform/case/config2d.mk"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("export DESIGN_NAME = case\n")
        self.seed = self.root / "seed"
        self.seed.mkdir()
        for name in flow.sa_flow.SEED_FILES:
            (self.seed / name).write_text(f"fixture seed {name}\n")
        executable = self.root / "openroad"
        executable.write_text(f'#!/bin/sh\nexec "{shutil.which("tclsh")}" "{REPO / "tests/unit/fixtures/sa_opendb.tcl"}" "$@"\n')
        executable.chmod(0o755)
        environment = mock.patch.dict(os.environ, {"OPENROAD_EXE": str(executable)})
        environment.start()
        self.addCleanup(environment.stop)
        self.output = self.root / "out"

    def generate(self, **options):
        settings = dict(seed_results=self.seed, method="min-cut", make_plots=False)
        settings.update(options)
        with redirect_stdout(io.StringIO()):
            return flow.generate(self.config, self.output, **settings)

    def test_tcl_exporter_geometry_io_and_power_filter(self):
        instances, nets = flow.export_snapshot(self.seed / "2_2_floorplan_io.odb",
                                               self.root / "graph.tsv", self.root / "graph.log")
        self.assertEqual([node.width * node.height for node in instances[:4]], [200, 50, 100, 6])
        self.assertEqual(sum(node.kind == "port" for node in instances), 8)
        self.assertNotIn(len(instances) - 1, {node for net in nets for node in net})
        self.assertEqual(len(nets), 6)  # Clock retained; power excluded.
        self.assertEqual(len(nets[2]), 10)  # No SA fanout filtering.
        self.assertEqual(nets[0], [0, 0, 1, 3])

    def test_export_and_shared_preparation_handoff(self):
        cache = self.root / "shared"
        count, output = self.generate(preparation_dir=cache)
        self.assertEqual(count, 1)
        document = json.loads((output / "candidates.json").read_text())
        record = next(iter(document["pareto_archive"]["solutions"].values()))
        assignments = dict(line.split() for line in (output / "solutions/s0000/macro_partition.txt").read_text().splitlines()
                           if line and not line.startswith("#"))
        self.assertEqual(assignments, {name: str(1 - group) for group, names in enumerate(record["solution"]) for name in names})
        self.assertEqual(len(assignments), 3)
        for name in flow.exporter.CC_SEED_FILES:
            self.assertEqual((output / "preparation/results" / name).read_bytes(), (self.seed / name).read_bytes())
        with (output / "metrics.tsv").open() as handle:
            row = next(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(row["method"], "min-cut")
        self.assertEqual(float(row["cut_weight"]), record["cost"][0])
        metadata = json.loads((output / "solutions/s0000/solution.json").read_text())
        self.assertEqual(metadata["metrics"], record["metrics"])
        self.assertTrue((cache / "results/2_2_floorplan_io.odb").is_file())

    def test_cached_seed_reuses_graph_and_changed_parameters_rebuild_in_memory(self):
        self.generate()
        with mock.patch.object(flow, "export_snapshot", side_effect=AssertionError("cache was not reused")):
            self.generate(seed_results=None, method="max-cut", top_k_ratio=0, k_hop=2, scale_factor=0.8)
        document = json.loads((self.output / "candidates.json").read_text())
        self.assertEqual(document["run"]["methods"], ["max-cut"])
        self.assertEqual(document["run"]["edge_count"], 6)

    def test_rerun_removes_only_stale_generated_files(self):
        self.generate(num_runs=2)
        unrelated = self.output / "solutions/s0001/evaluation.json"
        unrelated.write_text("preserve")
        self.generate()
        self.assertEqual(unrelated.read_text(), "preserve")
        self.assertFalse((unrelated.parent / "macro_partition.txt").exists())
        with (self.output / "manifest.tsv").open() as handle:
            self.assertEqual(len(list(csv.DictReader(handle, delimiter="\t"))), 1)

    @unittest.skipUnless(HAS_GNN, "GNN dependencies are optional")
    def test_all_three_algorithms_train_and_export_deterministically(self):
        options = dict(method="all", num_epochs=2, hidden_dim=8, embedding_dim=4, num_layers=1)
        count, _ = self.generate(**options)
        self.assertEqual(count, 3)
        before = (self.output / "candidates.json").read_bytes()
        self.generate(**options)
        self.assertEqual(before, (self.output / "candidates.json").read_bytes())
        records = json.loads(before)["pareto_archive"]["solutions"]
        self.assertEqual([record["method"] for record in records.values()], list(flow.METHODS))
        history = records["gnn-cut:seed42"]["loss_history"]
        self.assertEqual(len(history), 2)
        self.assertTrue(all(math.isfinite(value) for value in history))

    def test_invalid_options_do_not_prepare_a_design(self):
        for options in ({"scale_factor": 0}, {"top_k_ratio": math.nan}, {"k_hop": 0},
                        {"area_balance_error": -0.1}, {"num_runs": 0}, {"num_epochs": 0},
                        {"seed": -1}, {"dropout": 1}, {"threads": 0}):
            with mock.patch.object(flow.exporter, "prepare_macro_inventory") as prepare:
                with self.assertRaises(ValueError):
                    flow.generate(self.config, **options)
                prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
