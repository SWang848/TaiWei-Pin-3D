"""Arithmetic, annealing invariants, OpenDB adapter and TaiWei export contracts."""

import csv
from contextlib import redirect_stdout
import importlib.util
from itertools import combinations, product
import io
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
import generate_macro_partitions as exporter
import generate_sa_macro_partitions as sa


GRAPH = """# taiwei-sa-graph-v1
hierarchy\t/
node\t0\ttop/group/a\tram_a\t1\t200
node\t1\ttop/group/b\tram_b\t1\t50
node\t2\ttop/other/c\tram_c\t1\t100
node\t3\tlogic0\tcore\t0\t6
net\t4\t0\t0\t1\t3
net\t2\t1\t2
net\t10\t0\t1\t3
net\t9\t0\t2
net\t2\t1\t3
net\t0
"""
AREAS = {0: 200, 1: 50, 2: 100, 3: 6}
NETS = [(4, [0, 0, 1, 3]), (2, [1, 2]), (10, [0, 1, 3]), (9, [0, 2]), (2, [1, 3])]


def naive_metrics(solution):
    """Oracle from raw pin lists, without using Graph adjacency or SA deltas."""
    upper = set(solution[1])
    cut = sum((u in upper) != (v in upper) for count, pins in NETS if count < 10
              for u, v in combinations(pins, 2))
    areas = [sum(area for i, area in AREAS.items() if i not in upper),
             sum(AREAS[i] for i in upper)]
    return cut, abs(areas[0] - areas[1]), areas


class GraphAndAnnealingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "graph.tsv"
        self.path.write_text(GRAPH)
        self.graph = sa.graph_construction(self.path)

    def test_clique_multiplicity_self_loops_and_io_fanout(self):
        self.assertEqual(self.graph.adjacency[0], {1: 2, 3: 2, 2: 1})
        self.assertEqual(self.graph.adjacency[1], {0: 2, 3: 2, 2: 1})
        self.assertEqual(sum(map(lambda row: sum(row.values()), self.graph.adjacency.values())) // 2, 8)
        self.assertEqual(self.graph.metrics([[0, 2], [1]]), (5, 256, [306, 50]))

    def test_net_limit_is_exclusive_and_configurable(self):
        small = sa.graph_construction(self.path, net_pin_limit=9)
        large = sa.graph_construction(self.path, net_pin_limit=11)
        self.assertNotIn(2, small.adjacency[0])
        self.assertEqual(large.adjacency[0][1], 3)

    def test_all_partitions_and_hierarchy_moves_match_raw_pin_oracle(self):
        optimizer = sa.HMSA(self.graph)
        for labels in product((0, 1), repeat=3):
            solution = [[i for i, tier in enumerate(labels) if tier == group] for group in (0, 1)]
            self.assertEqual(self.graph.metrics(solution), naive_metrics(solution))
            for nodes in optimizer.hierarchy_tree.nodes_by_level.values():
                for node in nodes:
                    optimizer.reset_state_from_solution(solution)
                    before = naive_metrics(solution)
                    source = labels[node.macro_ids[0]]
                    moved = {i for i in node.macro_ids if labels[i] == source}
                    proposed = [list(group) for group in solution]
                    proposed[source] = [i for i in proposed[source] if i not in moved]
                    proposed[1 - source].extend(sorted(moved))
                    after = naive_metrics(proposed)
                    delta_cut, delta_area = optimizer.calculate_deltas(node)
                    self.assertEqual(delta_cut, after[0] - before[0])
                    self.assertEqual(delta_area, sum(AREAS[i] for i in moved))
                    optimizer.commit_move(node, delta_cut, delta_area)
                    self.assertEqual(optimizer.current_total_cutsize, after[0])
                    self.assertEqual(optimizer.current_imbalance, after[1])
                    self.assertEqual(optimizer.current_area_balance, after[2])
                    self.assertEqual(optimizer.partitions[3], sa.BOTTOM_DIE)

    def test_mixed_tier_group_uses_only_macros_that_actually_move(self):
        optimizer = sa.HMSA(self.graph)
        optimizer.reset_state_from_solution([[0, 2], [1]])
        node = next(n for n in optimizer.hierarchy_tree.nodes_by_level[2] if n.hierarchy_name == "top/group")
        self.assertEqual(optimizer.calculate_deltas(node), (1, 200))

    def test_hierarchy_uses_opendb_paths_without_splitting_literal_underscores(self):
        self.path.write_text(GRAPH.replace("top/group/a", "top/group/a__ram"))
        optimizer = sa.HMSA(sa.graph_construction(self.path))
        self.assertEqual(optimizer.hierarchy_tree.max_depth, 3)
        self.assertIn("top/group/a__ram", optimizer.hierarchy_tree.as_text())

    def test_flat_and_single_macro_designs(self):
        self.path.write_text("# taiwei-sa-graph-v1\nhierarchy\t/\nnode\t0\tram\tRAM\t1\t1\n")
        optimizer = sa.HMSA(sa.graph_construction(self.path))
        optimizer.run_annealing(2, 0.5, 0.5, 5)
        self.assertEqual(optimizer.hierarchy_tree.max_depth, 1)
        self.assertTrue(optimizer.candidate_document()["pareto_archive"]["solutions"])

    def test_annealing_checks_every_accepted_state_and_published_cost(self):
        for hierarchy in (True, False):
            optimizer = sa.HMSA(self.graph, grid_size=4, grid_cell_capacity=2)
            commit = optimizer.commit_move

            def checked_commit(*args):
                commit(*args)
                cut, imbalance, areas = naive_metrics(optimizer.current_solution)
                self.assertEqual(optimizer.current_total_cutsize, cut)
                self.assertEqual(optimizer.current_imbalance, imbalance)
                self.assertEqual(optimizer.current_area_balance, areas)

            optimizer.commit_move = checked_commit
            optimizer.run_annealing(4, 0.1, 0.5, 25, hierarchy)
            for record in optimizer.candidate_document()["pareto_archive"]["solutions"].values():
                by_name = {node.name: i for i, node in self.graph.nodes.items()}
                solution = [[by_name[name] for name in group] for group in record["solution"]]
                self.assertEqual(record["cost"], list(naive_metrics(solution)[:2]))
            for (x, y), entries in optimizer.pareto_archive_grid.items():
                self.assertTrue(0 <= x < 4 and 0 <= y < 4)
                self.assertLessEqual(len(entries), 2)

    def test_same_seed_is_deterministic_and_does_not_use_global_rng(self):
        first = sa.HMSA(self.graph, seed=18)
        second = sa.HMSA(self.graph, seed=18)
        first.run_annealing(4, 0.1, 0.5, 25)
        second.run_annealing(4, 0.1, 0.5, 25)
        self.assertEqual(first.candidate_document(), second.candidate_document())

    def test_fractional_areas_over_the_default_annealing_schedule(self):
        self.path.write_text(GRAPH.replace("\t200\n", "\t0.1\n").replace("\t50\n", "\t0.2\n")
                             .replace("\t100\n", "\t0.3\n").replace("\t6\n", "\t0.6\n"))
        graph = sa.graph_construction(self.path)
        optimizer = sa.HMSA(graph, grid_size=4)
        optimizer.run_annealing()
        cut, imbalance, areas = graph.metrics(optimizer.current_solution)
        self.assertEqual(optimizer.current_total_cutsize, cut)
        self.assertAlmostEqual(optimizer.current_imbalance, imbalance, places=10)
        for observed, expected in zip(optimizer.current_area_balance, areas):
            self.assertAlmostEqual(observed, expected, places=10)
        self.assertTrue(optimizer.candidate_document()["pareto_archive"]["solutions"])

    def test_grid_capacity_and_remapping_retain_snapshots(self):
        optimizer = sa.HMSA(self.graph, grid_size=1, grid_cell_capacity=2)
        optimizer.pareto_archive_grid.clear()
        for cost in ((2, 10), (3, 12), (4, 14)):
            optimizer._update_norm_bounds(*cost)
            optimizer._update_pareto_archive(cost, [[0], [1, 2]])
        self.assertEqual([entry[0] for entry in optimizer.pareto_archive_grid[(0, 0)]], [(3, 12), (4, 14)])
        optimizer.current_solution[0].clear()
        self.assertEqual(optimizer.pareto_archive_grid[(0, 0)][0][1], ((0,), (1, 2)))

    def test_true_pareto_archive_filters_dominated_states(self):
        optimizer = sa.HMSA(self.graph, grid_based_multi_objective=False)
        optimizer.pareto_archive_grid.clear()
        for cost in ((5, 10), (4, 12), (6, 11), (3, 9), (2, 20)):
            optimizer._update_pareto_archive(cost, [[0], [1, 2]])
        self.assertEqual(set(optimizer.pareto_archive_grid), {(3, 9), (2, 20)})
        optimizer.run_annealing(4, 0.1, 0.5, 25)
        costs = list(optimizer.pareto_archive_grid)
        self.assertFalse(any(optimizer._dominates(a, b) for a in costs for b in costs if a != b))

    def test_invalid_and_incomplete_graphs_are_rejected(self):
        for text in (GRAPH.replace("node\t1", "node\t0"),
                     GRAPH.replace("ram_a\t1\t200", "ram_a\t1\tnan"),
                     GRAPH.replace("net\t2\t1\t2", "net\t2\t1\t99"),
                     GRAPH.replace("\t1\t200", "\t0\t200").replace("\t1\t50", "\t0\t50").replace("\t1\t100", "\t0\t100")):
            self.path.write_text(text)
            with self.assertRaises(ValueError):
                sa.graph_construction(self.path)

    def test_solution_requires_disjoint_complete_macro_sets(self):
        optimizer = sa.HMSA(self.graph)
        for solution in ([[0], [1]], [[0, 0], [1, 2]], [[0, 1], [1, 2]], [[0, 3], [1, 2]], [[0, 1, 2], [], []]):
            with self.assertRaises(ValueError):
                optimizer.reset_state_from_solution(solution)

    def test_invalid_schedule_fails_before_preparing_a_design(self):
        for options in ({"T_max": math.nan}, {"T_max": 0.1}, {"T_min": 0}, {"gamma": 1},
                        {"gamma": 0}, {"steps_per_T": 0}, {"grid_size": 0}, {"grid_cell_capacity": 0},
                        {"net_pin_limit": 1}, {"hierarchy_separator": ""}):
            with mock.patch.object(exporter, "prepare_macro_inventory") as prepare:
                with self.assertRaises(ValueError):
                    sa.generate("missing.mk", **options)
                prepare.assert_not_called()


@unittest.skipUnless(shutil.which("tclsh"), "Tcl is needed to exercise the OpenDB adapter")
class FlowIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "designs/platform/case/config2d.mk"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("export DESIGN_NAME = case\n")
        self.seed = self.root / "seed"
        self.seed.mkdir()
        for name in sa.SEED_FILES:
            (self.seed / name).write_text(f"fixture seed {name}\n")
        self.openroad = self.root / "openroad"
        self.openroad.write_text(
            f'#!/bin/sh\nexec "{shutil.which("tclsh")}" "{REPO / "tests/unit/fixtures/sa_opendb.tcl"}" "$@"\n'
        )
        self.openroad.chmod(0o755)
        environment = mock.patch.dict(os.environ, {"OPENROAD_EXE": str(self.openroad), "PYTHONDONTWRITEBYTECODE": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.output = self.root / "out"

    def generate(self, **options):
        settings = dict(seed_results=self.seed, T_max=2.0, T_min=0.5,
                        gamma=0.5, steps_per_T=10, grid_size=3, grid_cell_capacity=2, make_plots=False)
        settings.update(options)
        return sa.generate(self.config, self.output, **settings)

    def test_real_tcl_exporter_converts_database_units_and_filters_power(self):
        graph = sa.export_graph(self.seed / "2_2_floorplan_io.odb", self.root / "graph.tsv", self.root / "graph.log")
        self.assertEqual({i: node.area for i, node in graph.nodes.items()}, AREAS)
        self.assertEqual(graph.adjacency, sa.graph_construction(self.write_graph()).adjacency)

    def write_graph(self):
        path = self.root / "expected.tsv"
        path.write_text(GRAPH)
        return path

    def test_new_cli_exports_same_files_as_existing_generator(self):
        command = [sys.executable, str(REPO / "generate_sa_macro_partitions.py"), str(self.config),
                   "--seed-results", str(self.seed), "--output-dir", str(self.output),
                   "--grid-size", "3", "--grid-cell-capacity", "2", "--t-max", "2", "--t-min", "0.5",
                   "--gamma", "0.5", "--steps-per-temperature", "10", "--no-plot"]
        result = subprocess.run(command, text=True, capture_output=True, check=False, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Generated", result.stdout)
        masters = exporter.read_macro_inventory(self.output / "preparation/macro_names.tsv")
        expected, count = exporter.prepare_files(self.output / "candidates.json", {}, masters)
        for relative, text in expected.items():
            self.assertEqual((self.output / relative).read_text(), text)
        with (self.output / "manifest.tsv").open() as handle:
            manifest = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(len(manifest), count)
        records = exporter.read_candidate_records(self.output / "candidates.json")
        for row in manifest:
            record = records[row["source_key"]]
            assignments = dict(line.split() for line in (self.output / row["partition_file"]).read_text().splitlines()
                               if not line.startswith("#"))
            self.assertEqual(assignments, {name: str(1 - tier) for tier, group in enumerate(record["solution"]) for name in group})
            ids = {"top/group/a": 0, "top/group/b": 1, "top/other/c": 2}
            self.assertEqual(record["cost"], list(naive_metrics([[ids[name] for name in group] for group in record["solution"]])[:2]))
        for name in sa.SEED_FILES:
            self.assertEqual((self.output / "preparation/results" / name).read_bytes(), (self.seed / name).read_bytes())
        state = json.loads((self.output / "preparation/seed_info.json").read_text())
        self.assertEqual(state["status"], "complete")
        self.assertTrue((self.output / "preparation/logs/sa_graph.log").is_file())

    def test_fresh_config_uses_existing_taiwei_preparation(self):
        def prepare(config, preparation, rebuild):
            self.assertEqual(config, self.config)
            self.assertFalse(rebuild)
            sa.import_seed_results(self.seed, preparation)
            return {"top/group/a": "ram_a", "top/group/b": "ram_b", "top/other/c": "ram_c"}

        with mock.patch.object(exporter, "prepare_macro_inventory", side_effect=prepare) as preparation:
            count, directory = sa.generate(self.config, self.output, T_max=2, T_min=0.5,
                                           gamma=0.5, steps_per_T=5, make_plots=False)
        preparation.assert_called_once()
        self.assertGreater(count, 0)
        self.assertEqual(directory, self.output)

    def test_custom_preparation_exports_cc_handoff_into_output(self):
        self.generate(preparation_dir=self.root / "cache")
        for name in exporter.CC_SEED_FILES:
            self.assertEqual((self.output / "preparation/results" / name).read_bytes(), (self.seed / name).read_bytes())
        self.assertTrue((self.root / "cache/results/sa_graph.tsv").is_file())

    def test_generated_output_is_accepted_by_cc_submission_dry_run(self):
        self.output = self.root / "sa-inputs/platform/case"
        count, _ = self.generate()
        scratch = self.root / "scratch"
        environment = {
            **os.environ, "TAIWEI_BASE": str(REPO), "CC_INPUT_ROOT": str(self.root / "sa-inputs"),
            "SCRATCH": str(scratch), "CC_OUTPUT_ROOT": str(scratch / "taiwei"),
            "CC_PLATFORM": "platform", "CC_CPUS": "1", "CC_MEMORY": "1G", "CC_TIME": "01:00:00",
            "CC_STD_CELL_TIER": "bottom", "CC_OUTER_ITERATIONS": "1", "CC_ACCOUNT": "",
        }
        result = subprocess.run([sys.executable, str(REPO / "cc_script/lib/submit.py"), "case", "2", "--dry-run"],
                                env=environment, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Prepared solutions: {count}", result.stdout)
        self.assertFalse(scratch.exists())

    def test_identical_repeat_preserves_partitions_and_reuses_graph(self):
        self.generate()
        partition = self.output / "solutions/s0000/macro_partition.txt"
        before = (partition.read_bytes(), partition.stat().st_mtime_ns)
        candidates = (self.output / "candidates.json").read_bytes()
        with mock.patch.object(sa, "export_graph") as export_graph:
            self.generate()
            export_graph.assert_not_called()
        self.assertEqual((partition.read_bytes(), partition.stat().st_mtime_ns), before)
        self.assertEqual((self.output / "candidates.json").read_bytes(), candidates)

    def test_new_parameters_replace_partitions_and_remove_only_stale_generated_files(self):
        count, _ = self.generate()
        self.assertGreater(count, 1)
        preparation = self.output / "preparation"
        before = {path.relative_to(preparation): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in preparation.rglob("*") if path.is_file()}
        stale = self.output / "solutions" / f"s{count - 1:04d}"
        notes = stale / "notes.txt"
        notes.write_text("keep these notes\n")
        unrelated = self.output / "runs/status.json"
        unrelated.parent.mkdir()
        unrelated.write_text("old evaluation\n")
        with mock.patch.object(sa, "export_graph") as export_graph:
            new_count, _ = self.generate(seed=900, grid_size=1, grid_cell_capacity=1,
                                         T_max=3.0, T_min=0.25, gamma=0.7, steps_per_T=13)
            export_graph.assert_not_called()
        self.assertEqual(new_count, 1)
        self.assertEqual(len(list((self.output / "solutions").glob("*/macro_partition.txt"))), 1)
        self.assertFalse((stale / "macro_partition.txt").exists())
        self.assertFalse((stale / "solution.json").exists())
        self.assertEqual(notes.read_text(), "keep these notes\n")
        self.assertEqual(unrelated.read_text(), "old evaluation\n")
        document = json.loads((self.output / "candidates.json").read_text())
        self.assertEqual(document["run"]["seed"], 900)
        self.assertEqual(document["run"]["steps_per_T"], 13)
        self.assertEqual(len(document["pareto_archive"]["solutions"]), 1)
        with (self.output / "manifest.tsv").open() as handle:
            self.assertEqual(len(list(csv.DictReader(handle, delimiter="\t"))), 1)
        after = {path.relative_to(preparation): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in preparation.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_every_annealing_mode_and_graph_cutoff_reuses_raw_graph(self):
        self.generate()
        graph_path = self.output / "preparation/results/sa_graph.tsv"
        before = (graph_path.read_bytes(), graph_path.stat().st_mtime_ns)
        with mock.patch.object(sa, "export_graph") as export_graph:
            self.generate(net_pin_limit=9, hierarchy_aware_move=False,
                          grid_based_multi_objective=False, hierarchy_separator="__")
            export_graph.assert_not_called()
        self.assertEqual((graph_path.read_bytes(), graph_path.stat().st_mtime_ns), before)
        graph = sa.graph_construction(graph_path, net_pin_limit=9)
        by_name = {node.name: i for i, node in graph.nodes.items()}
        records = exporter.read_candidate_records(self.output / "candidates.json")
        for record in records.values():
            ids = [[by_name[name] for name in group] for group in record["solution"]]
            self.assertEqual(record["cost"], list(graph.metrics(ids)[:2]))

    def test_legacy_graph_is_adopted_using_existing_archive_hashes(self):
        self.generate()
        state = self.output / "preparation/results/sa_graph_info.json"
        state.unlink()
        graph = self.output / "preparation/results/sa_graph.tsv"
        before = (graph.read_bytes(), graph.stat().st_mtime_ns)
        with mock.patch.object(sa, "export_graph") as export_graph:
            self.generate(seed=900)
            export_graph.assert_not_called()
        self.assertEqual((graph.read_bytes(), graph.stat().st_mtime_ns), before)
        self.assertTrue(state.is_file())

    def test_modified_graph_is_exported_again_before_replacing_partitions(self):
        self.generate()
        graph = self.output / "preparation/results/sa_graph.tsv"
        original = graph.read_text()
        graph.write_text(original + "damaged graph\n")
        with mock.patch.object(sa, "export_graph", wraps=sa.export_graph) as export_graph:
            self.generate(seed=900)
            export_graph.assert_called_once()
        self.assertEqual(graph.read_text(), original)

    def test_changed_seed_and_exporter_invalidate_graph_cache(self):
        self.generate()
        seed_db = self.output / "preparation/results/2_2_floorplan_io.odb"
        graph = self.output / "preparation/results/sa_graph.tsv"
        log = self.output / "preparation/logs/sa_graph.log"
        seed_db.write_text("updated fixture seed\n")
        with mock.patch.object(sa, "export_graph", wraps=sa.export_graph) as export_graph:
            sa.load_or_export_graph(seed_db, graph, log)
            export_graph.assert_called_once()
        tool_repo = self.root / "updated-tool"
        script = tool_repo / "scripts_openroad/export_sa_graph.tcl"
        script.parent.mkdir(parents=True)
        script.write_text((REPO / "scripts_openroad/export_sa_graph.tcl").read_text() + "\n# updated exporter\n")
        with mock.patch.object(sa, "REPO_ROOT", tool_repo):
            with mock.patch.object(sa, "export_graph", wraps=sa.export_graph) as export_graph:
                sa.load_or_export_graph(seed_db, graph, log)
                export_graph.assert_called_once()

    def test_new_output_directory_can_share_preparation_and_graph(self):
        self.generate()
        original_output = self.output
        archive = (original_output / "candidates.json").read_bytes()
        self.output = self.root / "new-output"
        with mock.patch.object(sa, "export_graph") as export_graph:
            self.generate(seed=900, preparation_dir=original_output / "preparation")
            export_graph.assert_not_called()
        self.assertEqual((original_output / "candidates.json").read_bytes(), archive)
        for name in exporter.CC_SEED_FILES:
            self.assertEqual((self.output / "preparation/results" / name).read_bytes(), (self.seed / name).read_bytes())

    def test_invalid_new_archive_preserves_the_previous_partition_outputs(self):
        self.generate()
        candidates = (self.output / "candidates.json").read_bytes()
        partitions = {path: path.read_bytes() for path in (self.output / "solutions").rglob("*") if path.is_file()}
        malformed = {"pareto_archive": {"solutions": {"bad": {"solution": [[], []], "cost": [0, 0]}}}}
        with mock.patch.object(sa.HMSA, "candidate_document", return_value=malformed):
            with self.assertRaisesRegex(ValueError, "missing macros"):
                self.generate(seed=900)
        self.assertEqual((self.output / "candidates.json").read_bytes(), candidates)
        self.assertEqual({path: path.read_bytes() for path in partitions}, partitions)

    def test_frontend_and_graph_tools_run_only_once_across_parameter_changes(self):
        real_run = subprocess.run
        calls = []

        def run(command, **kwargs):
            calls.append(command[0])
            if command[0] != "make":
                return real_run(command, **kwargs)
            values = dict(arg.split("=", 1) for arg in command if "=" in arg)
            preparation = Path(values["WORK_HOME"])
            sa.import_seed_results(self.seed, preparation)
            Path(values["MACRO_NAMES_FILE"]).write_text(
                "instance_name\tmaster_name\ntop/group/a\tram_a\ntop/group/b\tram_b\ntop/other/c\tram_c\n"
            )
            kwargs["stdout"].write("fixture synthesis and OpenDB macro inventory complete\n")
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(sa.subprocess, "run", side_effect=run):
            self.generate(seed_results=None)
            self.generate(seed_results=None, seed=900, grid_size=1, grid_cell_capacity=1, gamma=0.75)
        self.assertEqual(calls, ["make", str(self.openroad)])

    def test_incomplete_seed_is_rejected_before_openroad(self):
        (self.seed / "1_synth.sdc").unlink()
        with mock.patch.object(sa, "export_graph") as export_graph:
            with self.assertRaisesRegex(ValueError, "Incomplete 2D seed"):
                self.generate()
            export_graph.assert_not_called()
        self.assertFalse((self.output / "solutions").exists())

    def test_graph_failure_retains_previous_graph_and_does_not_export_solutions(self):
        preparation = self.output / "preparation"
        (preparation / "results").mkdir(parents=True)
        graph_path = preparation / "results/sa_graph.tsv"
        graph_path.write_text("old graph\n")
        self.openroad.write_text("#!/bin/sh\necho export-failed\nexit 4\n")
        with self.assertRaisesRegex(RuntimeError, "export-failed"):
            self.generate()
        self.assertEqual(graph_path.read_text(), "old graph\n")
        self.assertFalse((self.output / "candidates.json").exists())
        self.assertFalse((self.output / "solutions").exists())

    def test_missing_matplotlib_is_reported_before_preparation(self):
        with mock.patch.dict(sys.modules, {"matplotlib": None}):
            with mock.patch.object(exporter, "prepare_macro_inventory") as prepare:
                with self.assertRaisesRegex(RuntimeError, "Matplotlib"):
                    sa.generate(self.config, self.output)
                prepare.assert_not_called()
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Matplotlib is needed for rendering checks")
    def test_generation_saves_and_refreshes_plots_with_new_parameters(self):
        self.generate(make_plots=True)
        png = self.output / "partition_solutions.png"
        pdf = self.output / "partition_solutions.pdf"
        before = png.read_bytes()
        self.assertTrue(before.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
        self.generate(seed=900, grid_size=1, grid_cell_capacity=1, make_plots=True)
        self.assertNotEqual(png.read_bytes(), before)
        self.assertTrue(pdf.is_file())


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Matplotlib is needed for rendering checks")
class ArchivePlotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.records = {
            "a": {"cost": [100, 1000000], "solution": [[], []]},
            "b": {"cost": [150, 200000], "solution": [[], []]},
            "duplicate": {"cost": [100, 1000000], "solution": [[], []]},
            "dominated": {"cost": [200, 2000000], "solution": [[], []]},
        }
        self.candidates = self.root / "candidates.json"
        self.candidates.write_text(json.dumps({"pareto_archive": {"solutions": self.records}}))
        self.plt = sa.plotting_module()
        self.addCleanup(self.plt.close, "all")

    def test_scatter_preserves_raw_costs_duplicates_and_dominated_solutions(self):
        figure = sa.make_partition_plot(self.records)
        axis = figure.axes[0]
        self.assertEqual(axis.get_xlabel(), "Cut Size")
        self.assertEqual(axis.get_ylabel(), "Area Imbalance (µm²)")
        self.assertEqual(axis.get_xscale(), "linear")
        self.assertEqual(axis.get_yscale(), "linear")
        self.assertEqual(len(axis.collections), 1)
        self.assertEqual(axis.collections[0].get_offsets().tolist(), [record["cost"] for record in self.records.values()])
        self.assertEqual([text.get_text() for text in axis.texts], ["Points: 4"])

    def test_export_writes_png_and_pdf_and_preserves_candidate_archive(self):
        before = (self.candidates.read_bytes(), self.candidates.stat().st_mtime_ns)
        destination = self.root / "plots"
        png, pdf = sa.plot_partition_solutions(self.candidates, destination)
        self.assertEqual((png.name, pdf.name), sa.PLOT_FILES)
        self.assertTrue(png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
        self.assertEqual((self.candidates.read_bytes(), self.candidates.stat().st_mtime_ns), before)
        self.assertEqual(self.plt.get_fignums(), [])

    def test_plot_only_cli_needs_no_design_or_preparation(self):
        with mock.patch.object(sa, "generate") as generate:
            with redirect_stdout(io.StringIO()) as output:
                result = sa.main(["--plot-only", str(self.candidates)])
            generate.assert_not_called()
        self.assertEqual(result, 0)
        self.assertIn("Saved plot:", output.getvalue())
        self.assertTrue(all((self.root / name).is_file() for name in sa.PLOT_FILES))

    def test_zero_cost_single_solution_can_be_rendered(self):
        figure = sa.make_partition_plot({"only": {"cost": [0, 0]}})
        figure.canvas.draw()
        self.assertEqual(figure.axes[0].collections[0].get_offsets().tolist(), [[0, 0]])

    def test_empty_and_negative_costs_are_rejected(self):
        for records in ({}, {"bad": {"cost": [-1, 2]}}, {"bad": {"cost": [1, math.nan]}}):
            with self.assertRaises(ValueError):
                sa.make_partition_plot(records)


if __name__ == "__main__":
    unittest.main()
