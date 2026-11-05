"""Run with python3 -m unittest discover -s tests/unit -p test_generate_macro_partitions.py."""

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import generate_macro_partitions as generator


class GenerateMacroPartitionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.candidates = self.root / "candidate.json"
        self.records = {
            "(37, 21)": {"cost": [1232, 1501102.2609214783], "solution": [["top_a"], ["top_b"]]},
            "(9, 1)": {"cost": [900, 123.5], "solution": [["top_b"], ["top_a"]]},
        }
        self.write_candidates()
        self.write_map([("top_a", "top/a", "RAM"), ("top_b", "top/b", "RAM")])

    def write_candidates(self):
        self.candidates.write_text(json.dumps({"pareto_archive": {"solutions": self.records}}))

    def write_map(self, rows):
        with (self.root / "macro_name_map.tsv").open("w", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["external_name", "instance_name", "master_name"])
            writer.writerows(rows)

    def test_single_input_cli_creates_complete_layout(self):
        source = self.candidates.read_bytes()
        result = subprocess.run(
            [sys.executable, str(REPO / "generate_macro_partitions.py"), str(self.candidates)],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Generated 2 solution folders", result.stdout)
        self.assertEqual(self.candidates.read_bytes(), source)
        for index, (key, record) in enumerate(self.records.items()):
            solution_id = f"s{index:04d}"
            directory = self.root / "solutions" / solution_id
            metadata = json.loads((directory / "solution.json").read_text())
            self.assertEqual(metadata, {
                "solution_id": solution_id,
                "source_index": index,
                "source_key": key,
                "cost": record["cost"],
            })
            lines = (directory / "macro_partition.txt").read_text().splitlines()
            assignments = dict(line.split() for line in lines if not line.startswith("#"))
            self.assertEqual(assignments, {name.replace("_", "/"): str(1 - group_index)
                                         for group_index, group in enumerate(record["solution"]) for name in group})
        with (self.root / "manifest.tsv").open() as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual([row["source_key"] for row in rows], list(self.records))
        for row in rows:
            self.assertTrue((self.root / row["partition_file"]).is_file())
            self.assertTrue((self.root / row["metadata_file"]).is_file())

    def test_refresh_simplifies_old_metadata_and_preserves_partitions_and_runs(self):
        generator.generate(self.candidates)
        metadata_file = self.root / "solutions/s0000/solution.json"
        old = json.loads(metadata_file.read_text())
        old["source_sha256"] = "old tracking field"
        old["original_record"] = self.records["(37, 21)"]
        old.pop("cost")
        metadata_file.write_text(json.dumps(old))
        partition = self.root / "solutions/s0000/macro_partition.txt"
        before = (partition.read_bytes(), partition.stat().st_mtime_ns)
        status = self.root / "runs/s0000/a00/status.json"
        status.parent.mkdir(parents=True)
        status.write_text('{"status": "completed"}')
        generator.generate(self.candidates)
        self.assertEqual(set(json.loads(metadata_file.read_text())), {"solution_id", "source_index", "source_key", "cost"})
        self.assertEqual(before, (partition.read_bytes(), partition.stat().st_mtime_ns))
        self.assertEqual(status.read_text(), '{"status": "completed"}')

    def test_missing_macro_in_later_record_does_not_write_partial_outputs(self):
        self.records["(9, 1)"]["solution"] = [["top_a"], []]
        self.write_candidates()
        with self.assertRaisesRegex(ValueError, "missing macros"):
            generator.generate(self.candidates)
        self.assertFalse((self.root / "solutions").exists())
        self.assertFalse((self.root / "manifest.tsv").exists())

    def test_duplicate_macro_is_rejected(self):
        self.records["(37, 21)"]["solution"] = [["top_a"], ["top_a", "top_b"]]
        self.write_candidates()
        with self.assertRaisesRegex(ValueError, "assigned more than once"):
            generator.generate(self.candidates)

    def test_candidate_group_zero_is_bottom_and_group_one_is_upper(self):
        self.write_map([
            ("top_a", "top/a", "RAM"), ("top_b", "top/b", "RAM"),
            ("top_c", "top/c", "RAM"),
        ])
        self.records = {
            "bottom_first": {"cost": [7, 8.5], "solution": [["top_a", "top_b"], ["top_c"]]},
        }
        self.write_candidates()
        generator.generate(self.candidates)
        partition = self.root / "solutions/s0000/macro_partition.txt"
        assignments = dict(line.split() for line in partition.read_text().splitlines()
                           if line and not line.startswith("#"))
        self.assertEqual(assignments, {"top/a": "1", "top/b": "1", "top/c": "0"})
        with (self.root / "manifest.tsv").open() as handle:
            row = next(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual((row["upper_macros"], row["bottom_macros"]), ("1", "2"))

    def test_ambiguous_flattened_names_are_rejected(self):
        self.write_map([("known_1", "top/a_b", "RAM"), ("known_2", "top_a/b", "RAM")])
        self.records["(37, 21)"]["solution"] = [["top_a_b"], ["known_2"]]
        self.write_candidates()
        with self.assertRaisesRegex(ValueError, "unknown or ambiguous macro"):
            generator.generate(self.candidates)

    def test_source_reordering_cannot_overwrite_other_solution_identities(self):
        generator.generate(self.candidates)
        metadata = self.root / "solutions/s0000/solution.json"
        before = metadata.read_bytes()
        self.records = dict(reversed(list(self.records.items())))
        self.write_candidates()
        with self.assertRaisesRegex(ValueError, "Solution identity differs"):
            generator.generate(self.candidates)
        self.assertEqual(metadata.read_bytes(), before)

    def test_stale_solution_directories_require_a_new_output_directory(self):
        generator.generate(self.candidates)
        del self.records["(9, 1)"]
        self.write_candidates()
        with self.assertRaisesRegex(ValueError, "Stale solution folder"):
            generator.generate(self.candidates)
        self.assertTrue((self.root / "solutions/s0001/solution.json").exists())

    def test_explicit_reference_partition_supports_checked_alias_conversion(self):
        reference = self.root / "reference.txt"
        reference.write_text("top/a 0\ntop/b 1\n")
        count, output = generator.generate(self.candidates, reference_partition=reference)
        self.assertEqual(count, 2)
        self.assertEqual(output, self.root)
        self.assertIn("top/a 1", (output / "solutions/s0000/macro_partition.txt").read_text())

    def test_separate_output_directory_preserves_source_directory(self):
        output = self.root / "exported"
        generator.generate(self.candidates, output_dir=output)
        self.assertTrue((output / "solutions/s0000/solution.json").is_file())
        self.assertFalse((self.root / "solutions").exists())

    def test_nonfinite_and_boolean_costs_are_rejected(self):
        for value in (float("inf"), float("nan"), True):
            with self.subTest(value=value):
                self.records["(37, 21)"]["cost"] = [1232, value]
                self.write_candidates()
                with self.assertRaises(ValueError):
                    generator.generate(self.candidates)
        self.assertFalse((self.root / "solutions").exists())

    def test_duplicate_archive_keys_are_rejected(self):
        self.candidates.write_text('{"pareto_archive": {"solutions": {}, "solutions": {}}}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            generator.generate(self.candidates)


class FreshDesignPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.case = self.repo / "partition_experiments/new_platform/new_case"
        self.case.mkdir(parents=True)
        self.config = self.repo / "designs/new_platform/new_case/config2d.mk"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("export DESIGN_NAME = new_case\nexport PLATFORM = test2d\n")
        self.candidates = self.case / "candidates.json"
        self.candidates.write_text(json.dumps({"pareto_archive": {"solutions": {
            "new": {"cost": [12, 34.5], "solution": [["top_a"], ["top_b"]]},
        }}}))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(generator, "REPO_ROOT", self.repo).start()
        self.commands = []

    def successful_make(self, command, **kwargs):
        self.commands.append(command)
        self.assertEqual(command[-3:], ["ord-synth", "ord-preplace", "ord-export-macro-names"])
        self.assertIn("ord-check-macro-tools", command)
        self.assertNotIn("ord-tier-partition", command)
        self.assertNotIn("ord-3d-flow-2dpart", command)
        values = dict(arg.split("=", 1) for arg in command if "=" in arg)
        preparation = self.case / "preparation"
        for variable, directory in (("RESULTS_DIR", "results"), ("LOG_DIR", "logs"),
                                    ("OBJECTS_DIR", "objects"), ("REPORTS_DIR", "reports")):
            self.assertEqual(Path(values[variable]), preparation / directory)
            Path(values[variable]).mkdir(parents=True, exist_ok=True)
        self.assertEqual(values["MACRO_PARTITION_FILE"], "")
        for name in ("1_synth.sdc", "2_2_floorplan_io.odb", "2_2_floorplan_io.def", "2_2_floorplan_io.v"):
            (Path(values["RESULTS_DIR"]) / name).write_text("mock 2D tool output\n")
        Path(values["MACRO_NAMES_FILE"]).write_text(
            "instance_name\tmaster_name\ntop/a\tNEW_SRAM_A\ntop/b\tNEW_SRAM_B\n"
        )
        kwargs["stdout"].write("mock synthesis, floorplan, IO and OpenDB export complete\n")
        return subprocess.CompletedProcess(command, 0)

    def test_fresh_design_without_any_name_reference_prepares_and_exports(self):
        self.assertFalse((self.case / "macro_name_map.tsv").exists())
        self.assertFalse((self.repo / "results").exists())
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            count, output = generator.generate(self.candidates)
        self.assertEqual(count, 1)
        partition = (output / "solutions/s0000/macro_partition.txt").read_text()
        self.assertIn("top/a 1\ntop/b 0\n", partition)
        metadata = json.loads((output / "solutions/s0000/solution.json").read_text())
        self.assertEqual(set(metadata), {"solution_id", "source_index", "source_key", "cost"})
        with (output / "macro_name_map.tsv").open() as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual([row["master_name"] for row in rows], ["NEW_SRAM_A", "NEW_SRAM_B"])
        self.assertEqual(len(self.commands), 1)
        self.assertFalse((self.repo / "results").exists())
        self.assertFalse((self.case / "preparation").exists())

    def test_completed_seed_is_cached_and_rebuild_flag_runs_preparation_again(self):
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            generator.generate(self.candidates, keep_preparation=True)
            generator.generate(self.candidates, keep_preparation=True)
            self.assertEqual(len(self.commands), 1)
            generator.generate(self.candidates, rebuild_seed=True, keep_preparation=True)
        self.assertEqual(len(self.commands), 2)

    def test_config_change_invalidates_the_cached_seed(self):
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            generator.generate(self.candidates, keep_preparation=True)
            self.config.write_text(self.config.read_text() + "export CORE_UTILIZATION = 50\n")
            generator.generate(self.candidates, keep_preparation=True)
        self.assertEqual(len(self.commands), 2)

    def test_modified_inventory_is_not_trusted_as_a_loaded_seed(self):
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            generator.generate(self.candidates, keep_preparation=True)
            inventory = self.case / "preparation/macro_names.tsv"
            inventory.write_text(inventory.read_text().replace("top/a", "invented/a"))
            generator.generate(self.candidates, keep_preparation=True)
        self.assertEqual(len(self.commands), 2)
        self.assertIn("top/a", inventory.read_text())

    def test_failed_preparation_cannot_generate_solutions_or_be_reused(self):
        def failed_make(command, **kwargs):
            kwargs["stdout"].write("OpenROAD toolchain unavailable\n")
            return subprocess.CompletedProcess(command, 2)
        with mock.patch.object(generator.subprocess, "run", side_effect=failed_make):
            with self.assertRaisesRegex(RuntimeError, "2D preparation failed"):
                generator.generate(self.candidates)
        self.assertFalse((self.case / "solutions").exists())
        state = json.loads((self.case / "preparation/seed_info.json").read_text())
        self.assertEqual(state["status"], "preparing")
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            generator.generate(self.candidates)
        self.assertEqual(len(self.commands), 1)

    def test_malformed_candidates_do_not_start_eda_tools(self):
        self.candidates.write_text('{"pareto_archive": {"solutions": {}}}')
        with mock.patch.object(generator.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "nonempty"):
                generator.generate(self.candidates)
            run.assert_not_called()
        self.assertFalse((self.case / "preparation").exists())

    def test_unknown_candidate_is_rejected_against_loaded_seed_catalog(self):
        text = self.candidates.read_text().replace("top_a", "made_up_macro")
        self.candidates.write_text(text)
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            with self.assertRaisesRegex(ValueError, "unknown or ambiguous"):
                generator.generate(self.candidates)
        self.assertFalse((self.case / "solutions").exists())
        self.assertTrue((self.case / "preparation/prepare.log").is_file())

    def test_cached_preparation_is_removed_after_a_successful_default_run(self):
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            generator.generate(self.candidates, keep_preparation=True)
            self.assertTrue((self.case / "preparation").is_dir())
            generator.generate(self.candidates)
        self.assertEqual(len(self.commands), 1)
        self.assertFalse((self.case / "preparation").exists())
        self.assertTrue((self.case / "solutions/s0000/macro_partition.txt").is_file())
        self.assertTrue((self.case / "manifest.tsv").is_file())
        self.assertTrue((self.case / "macro_name_map.tsv").is_file())

    def test_failed_export_retains_preparation_and_logs(self):
        original_write = generator.write_if_changed
        def fail_solution_write(path, text):
            if path.name == "solution.json":
                raise OSError("test output write failure")
            original_write(path, text)
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            with mock.patch.object(generator, "write_if_changed", side_effect=fail_solution_write):
                with self.assertRaisesRegex(OSError, "output write failure"):
                    generator.generate(self.candidates)
        self.assertTrue((self.case / "preparation/prepare.log").is_file())
        self.assertTrue((self.case / "preparation/results/2_2_floorplan_io.odb").is_file())

    def test_offline_export_does_not_remove_an_unused_preparation_directory(self):
        reference = self.case / "reference.txt"
        reference.write_text("top/a 0\ntop/b 1\n")
        preparation = self.case / "preparation"
        preparation.mkdir()
        marker = preparation / "existing.txt"
        marker.write_text("retain unused files")
        generator.generate(self.candidates, reference_partition=reference)
        self.assertEqual(marker.read_text(), "retain unused files")

    def test_cleanup_cannot_target_output_or_source_directories(self):
        for unsafe in (self.case, self.repo, self.config.parent):
            with self.subTest(unsafe=unsafe):
                with mock.patch.object(generator.subprocess, "run") as run:
                    with self.assertRaisesRegex(ValueError, "Preparation directory must not contain"):
                        generator.generate(self.candidates, preparation_dir=unsafe)
                    run.assert_not_called()
        self.assertTrue(self.candidates.is_file())
        self.assertTrue(self.config.is_file())

    def test_cleanup_rejects_unrelated_files_in_preparation_directory(self):
        preparation = self.case / "preparation"
        preparation.mkdir()
        marker = preparation / "important.txt"
        marker.write_text("retain unrelated files")
        with mock.patch.object(generator.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "contains unrelated files"):
                generator.generate(self.candidates)
            run.assert_not_called()
        self.assertEqual(marker.read_text(), "retain unrelated files")

    def test_lifecycle_lock_remains_effective_after_preparation_is_deleted(self):
        preparation = self.case / "preparation"
        preparation.mkdir()
        lock_path = self.case / ".preparation.lock"
        child = (
            "import fcntl, sys\n"
            "with open(sys.argv[1], 'a') as lock:\n"
            "    try:\n"
            "        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except BlockingIOError:\n"
            "        sys.exit(3)\n"
        )
        with generator.preparation_lifecycle_lock(preparation):
            preparation.rmdir()
            result = subprocess.run([sys.executable, "-c", child, str(lock_path)], check=False)
            self.assertEqual(result.returncode, 3)
        result = subprocess.run([sys.executable, "-c", child, str(lock_path)], check=False)
        self.assertEqual(result.returncode, 0)

    def test_existing_name_map_cannot_replace_the_loaded_seed_catalog(self):
        (self.case / "macro_name_map.tsv").write_text(
            "external_name\tinstance_name\tmaster_name\n"
            "top_a\told/a\tSTALE_MASTER\n"
            "top_b\told/b\tSTALE_MASTER\n"
        )
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            generator.generate(self.candidates)
        self.assertIn("top/a 1", (self.case / "solutions/s0000/macro_partition.txt").read_text())
        self.assertNotIn("STALE_MASTER", (self.case / "macro_name_map.tsv").read_text())

    def test_explicit_design_config_supports_candidates_outside_case_layout(self):
        external = self.repo / "candidate.json"
        external.write_bytes(self.candidates.read_bytes())
        with mock.patch.object(generator.subprocess, "run", side_effect=self.successful_make):
            generator.generate(external, output_dir=self.case, design_config=self.config)
        self.assertEqual(len(self.commands), 1)


class LoadedMacroExporterTests(unittest.TestCase):
    def test_tcl_exporter_uses_loaded_instance_names_and_filters_standard_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "2_2_floorplan_io.odb").write_text("mock database")
            output = root / "inventory.tsv"
            script = """
proc read_db {path} {if {![file isfile $path]} {error "missing database"}}
namespace eval ord {}
proc ord::get_db_block {} {return block}
proc block {method} {return {ram logic}}
proc ram {method} {
    if {$method eq "getName"} {return {hier/mem_bank[7]/u_sram}}
    return memory_master
}
proc logic {method} {return core_master}
proc memory_master {method} {
    if {$method eq "getType"} {return BLOCK}
    return CUSTOM_SRAM
}
proc core_master {method} {return CORE}
"""
            script += f"set ::env(RESULTS_DIR) {{{root}}}\n"
            script += f"set ::env(MACRO_NAMES_FILE) {{{output}}}\n"
            script += f"if {{[catch {{source {{{REPO / 'scripts_openroad/export_macro_names.tcl'}}}}} err]}} {{puts stderr $err; exit 1}}\n"
            result = subprocess.run(["tclsh"], input=script, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(generator.read_macro_inventory(output), {"hier/mem_bank[7]/u_sram": "CUSTOM_SRAM"})


if __name__ == "__main__":
    unittest.main()
