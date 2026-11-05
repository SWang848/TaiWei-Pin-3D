"""Exercise Slurm planning and candidate isolation without a scheduler or EDA tools."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


class ComputeCanadaScriptsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "TaiWei-Pin-3D"
        shutil.copytree(REPO_ROOT / "cc_script", self.repo / "cc_script")
        shutil.copyfile(REPO_ROOT / "generate_macro_partitions.py", self.repo / "generate_macro_partitions.py")
        design_dir = self.repo / "designs/nangate45_3D/bp_be"
        design_dir.mkdir(parents=True)
        for filename in ("config.mk", "config2d.mk"):
            (design_dir / filename).write_text("# fixture\n")
        self.case_dir = self.repo / "partition_experiments/nangate45_3D/bp_be"
        self.case_dir.mkdir(parents=True)
        self.candidates = {"pareto_archive": {"solutions": {
            "key\twith\nseparators": {"solution": [["ram_a"], ["ram_b"]], "cost": [1, 2]},
            "second": {"solution": [["ram_b"], ["ram_a"]], "cost": [3, 4]},
        }}}
        (self.case_dir / "candidates.json").write_text(json.dumps(self.candidates))
        (self.base / "containers").mkdir()
        (self.base / "containers/orfs.sif").write_text("SIF fixture")
        (self.base / "ORFS-Research/tools/install").mkdir(parents=True)
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir()
        self.environment = {
            **os.environ, "CC_BASE": str(self.base), "CC_ACCOUNT": "aip-test",
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "CC_TEST_CALLS": str(self.base / "sbatch.jsonl"),
            "CC_TEST_CANCELS": str(self.base / "cancelled.txt"),
        }
        self.environment.pop("CC_IMAGE", None)
        self.environment.pop("CC_TEST_FAIL_ARRAY", None)
        self.write_command("sbatch", f"""#!{sys.executable}
import json, os, pathlib, sys
path = pathlib.Path(os.environ['CC_TEST_CALLS'])
calls = path.read_text().splitlines() if path.exists() else []
with path.open('a') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('CC_TEST_FAIL_ARRAY') and any(arg.startswith('--array=') for arg in sys.argv):
    print('mock array submission rejected', file=sys.stderr)
    sys.exit(1)
print(1001 + len(calls))
""")
        self.write_command("scancel", "#!/bin/bash\nprintf '%s\\n' \"$@\" >> \"$CC_TEST_CANCELS\"\n")

    def write_command(self, name, text):
        path = self.bin_dir / name
        path.write_text(text)
        path.chmod(0o755)

    def submit(self, *arguments):
        return subprocess.run(
            [sys.executable, str(self.repo / "cc_script/submit_partitions.py"), "--case", "bp_be", *arguments],
            env=self.environment, text=True, capture_output=True,
        )

    def submission_calls(self):
        return [json.loads(line) for line in (self.base / "sbatch.jsonl").read_text().splitlines()]

    def test_dry_run_and_invalid_parallelism_do_not_submit(self):
        preview = self.submit("--dry-run", "--max-parallel", "2")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("--array=0-1%2", preview.stdout)
        self.assertFalse((self.case_dir / "cc_runs").exists())
        self.assertFalse((self.base / "sbatch.jsonl").exists())
        rejected = self.submit("--max-parallel", "0")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse((self.base / "sbatch.jsonl").exists())

    def test_submission_snapshots_inputs_and_queues_dependency(self):
        result = self.submit("--max-parallel", "2", "--cpus-per-task", "6", "--std-cell-tier", "upper")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.submission_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("--array=0-1%2", calls[1])
        self.assertIn("--dependency=afterok:1001", calls[1])
        self.assertIn("--kill-on-invalid-dep=yes", calls[1])
        self.assertTrue(all("--cpus-per-task=6" in call for call in calls))
        run_dir, = (self.case_dir / "cc_runs").iterdir()
        snapshot = json.loads((run_dir / "candidates.json").read_text())
        self.assertEqual(snapshot, self.candidates)
        metadata = json.loads((run_dir / "submission.json").read_text())
        self.assertEqual(metadata["jobs"], {"prepare": "1001", "array": "1002"})
        self.assertEqual([row["solution_id"] for row in metadata["solutions"]], ["s0000", "s0001"])
        self.assertEqual(metadata["solutions"][0]["source_key"], "key\twith\nseparators")

    def test_array_submission_failure_cancels_only_its_preparation(self):
        self.environment["CC_TEST_FAIL_ARRAY"] = "1"
        result = self.submit()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.base / "cancelled.txt").read_text(), "1001\n")

    def make_worker_fixture(self):
        run_dir = self.case_dir / "cc_runs/fixture"
        seed = run_dir / "preparation/results"
        seed.mkdir(parents=True)
        for name in ("1_synth.sdc", "2_2_floorplan_io.def", "2_2_floorplan_io.v", "2_2_floorplan_io.odb"):
            (seed / name).write_text(f"seed: {name}\n")
        for index in range(2):
            solution = run_dir / f"solutions/s{index:04d}"
            solution.mkdir(parents=True)
            (solution / "macro_partition.txt").write_text(f"ram_a {index}\nram_b {1-index}\n")
        (self.repo / "env.sh").write_text(":\n")
        launcher = self.repo / "test/openroad/ORD_3D_NEW_FLOW.sh"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("""#!/bin/bash
set -euo pipefail
[[ "$SKIP_2D_PART" == 1 && "$PIN3D_VIEW_IMPL" == tcl ]]
[[ ! -v START_FROM && ! -v REUSE_2DPART_FROM_VARIANT ]]
directory="reports/$1/$4/$2"
mkdir -p "$directory"
printf '%s\\t%s\\t%s\\n' "$MACRO_PARTITION_FILE" "$STD_CELL_TIER" "$OUTER_ITERATIONS" > "$directory/launcher.tsv"
if [[ "${CC_TEST_FLOW_EXIT:-0}" != 0 ]]; then exit "$CC_TEST_FLOW_EXIT"; fi
if [[ "${CC_TEST_NO_METRICS:-0}" != 1 ]]; then printf '{}\\n' > "$directory/openroad_eval.json"; fi
""")
        return run_dir

    def run_worker(self, index, variant):
        return subprocess.run(
            ["bash", str(self.repo / "cc_script/container_job.sh"), "run", "nangate45_3D", "bp_be",
             "partition_experiments/nangate45_3D/bp_be/cc_runs/fixture", "upper", "2", str(index), variant],
            env={**self.environment, "START_FROM": "ord-route", "REUSE_2DPART_FROM_VARIANT": "wrong"},
            text=True, capture_output=True,
        )

    def test_workers_isolate_outputs_reuse_seed_and_refuse_overwrite(self):
        run_dir = self.make_worker_fixture()
        for index in range(2):
            variant = f"cc_1002_s{index:04d}_r0"
            result = self.run_worker(index, variant)
            self.assertEqual(result.returncode, 0, result.stderr)
            result_dir = self.repo / f"results/nangate45_3D/bp_be/{variant}"
            self.assertEqual((result_dir / "1_synth.sdc").read_text(), "seed: 1_synth.sdc\n")
            source = run_dir / f"solutions/s{index:04d}/macro_partition.txt"
            self.assertEqual((result_dir / "partition.txt").read_text(), source.read_text())
            status = run_dir / f"task_status/s{index:04d}_{variant}.tsv"
            previous_status = status.read_text()
            self.assertIn("\tCOMPLETED\t0\t", previous_status)
            duplicate = self.run_worker(index, variant)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual(status.read_text(), previous_status)

    def test_worker_failure_is_propagated_and_missing_metrics_are_not_success(self):
        run_dir = self.make_worker_fixture()
        self.environment["CC_TEST_FLOW_EXIT"] = "37"
        failed = self.run_worker(0, "cc_failure")
        self.assertEqual(failed.returncode, 37, failed.stderr)
        self.assertIn("\tFAILED\t37\t", (run_dir / "task_status/s0000_cc_failure.tsv").read_text())
        self.environment["CC_TEST_FLOW_EXIT"] = "0"
        self.environment["CC_TEST_NO_METRICS"] = "1"
        missing = self.run_worker(1, "cc_missing_metrics")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("\tFAILED\t1\t", (run_dir / "task_status/s0001_cc_missing_metrics.tsv").read_text())

    def test_array_batch_reuses_sif_and_forwards_allocated_threads(self):
        self.write_command("module", "#!/bin/bash\nexit 0\n")
        self.write_command("apptainer", "#!/bin/bash\nprintf '%s\\n' \"$@\"\n")
        result = subprocess.run(
            ["bash", str(self.repo / "cc_script/run_partitions.sbatch"), "nangate45_3D", "bp_be", "some/run", "bottom", "1"],
            env={**self.environment, "SLURM_JOB_ID": "2003", "SLURM_ARRAY_JOB_ID": "2000",
                 "SLURM_ARRAY_TASK_ID": "3", "SLURM_CPUS_PER_TASK": "6", "SLURM_RESTART_COUNT": "2"},
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = result.stdout.splitlines()
        self.assertEqual(arguments[0], "exec")
        self.assertNotIn("build", arguments)
        self.assertIn("NUM_CORES=6", arguments)
        self.assertIn("OMP_NUM_THREADS=6", arguments)
        self.assertIn("cc_2000_s0003_r2", arguments)
        self.assertIn(str(self.base / "containers/orfs.sif"), arguments)


if __name__ == "__main__":
    unittest.main()
