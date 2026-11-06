"""Exercise Slurm planning and candidate isolation without a scheduler or EDA tools."""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


class ComputeCanadaScriptsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "TaiWei-Pin-3D"
        shutil.copytree(REPO_ROOT / "cc_script", self.repo / "cc_script")
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
        self.seed = self.case_dir / "preparation/results"
        self.seed.mkdir(parents=True)
        for name in ("1_synth.sdc", "2_2_floorplan_io.def", "2_2_floorplan_io.v"):
            (self.seed / name).write_text(f"seed: {name}\n")
        for index, (key, record) in enumerate(self.candidates["pareto_archive"]["solutions"].items()):
            solution = self.case_dir / f"solutions/s{index:04d}"
            solution.mkdir(parents=True)
            rows = [f"{name} {1-tier}\n" for tier, names in enumerate(record["solution"]) for name in names]
            (solution / "macro_partition.txt").write_text("".join(rows))
            (solution / "solution.json").write_text(json.dumps({
                "solution_id": solution.name, "source_key": key, "cost": record["cost"],
            }))
        (self.base / "containers").mkdir()
        (self.base / "containers/orfs.sif").write_text("SIF fixture")
        (self.base / "ORFS-Research/tools/install").mkdir(parents=True)
        for directory in ("platforms", "scripts_openroad", "util", "test"):
            (self.repo / directory).mkdir(exist_ok=True)
        (self.repo / "Makefile").write_text("# fixture\n")
        (self.repo / "env.sh").write_text(":\n")
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
        self.environment["CC_TEST_APPTAINER_CALLS"] = str(self.base / "apptainer.jsonl")
        self.environment["CC_TEST_STARTED"] = str(self.base / "flow-started")
        self.config = {
            "CC_BASE": str(self.base), "TAIWEI_BASE": str(self.repo),
            "ORFS_BASE": str(self.base / "ORFS-Research"),
            "CC_IMAGE": str(self.base / "containers/orfs.sif"), "CC_ACCOUNT": "aip-test",
            "CC_PLATFORM": "nangate45_3D", "CC_CPUS": "8", "CC_MEMORY": "32G",
            "CC_TIME": "12:00:00",
            "CC_STD_CELL_TIER": "bottom", "CC_OUTER_ITERATIONS": "1",
        }
        self.write_config()
        self.write_command("module", "#!/bin/bash\nexit 0\n")
        self.write_command("apptainer", f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
assert args[0] == 'exec' and 'build' not in args
binds, environment = {{}}, os.environ.copy()
for index, arg in enumerate(args[:-1]):
    if arg == '--bind':
        source, target, *options = args[index + 1].split(':')
        binds[target] = source
    if arg == '--env':
        key, value = args[index + 1].split('=', 1)
        environment[key] = value
repo = pathlib.Path(binds['/TaiWei-Pin-3D'])
assert str(repo).startswith(os.environ['SLURM_TMPDIR'] + '/')
assert binds['/ORFS-Research'].startswith(os.environ['SLURM_TMPDIR'] + '/')
with open(os.environ['CC_TEST_APPTAINER_CALLS'], 'a') as handle:
    handle.write(json.dumps({{'repo': str(repo), 'args': args}}) + '\\n')
start = args.index('/TaiWei-Pin-3D/cc_script/lib/container.sh') + 1
job_args = args[start:]
assert len(job_args) == 7, job_args
sys.exit(subprocess.run(['bash', str(repo / 'cc_script/lib/container.sh'), *job_args], env=environment).returncode)
""")
        self.write_command("sbatch", f"""#!{sys.executable}
import json, os, pathlib, sys
path = pathlib.Path(os.environ['CC_TEST_CALLS'])
calls = path.read_text().splitlines() if path.exists() else []
with path.open('a') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('CC_TEST_FAIL_ARRAY') and any(arg.startswith('--array=') for arg in sys.argv):
    print('mock array submission rejected', file=sys.stderr)
    sys.exit(1)
if os.environ.get('CC_TEST_FAIL_METADATA'):
    output = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--output='))
    (pathlib.Path(output).parent.parent / 'submission.json').mkdir()
print(1001 + len(calls))
""")
        self.write_command("scancel", "#!/bin/bash\nprintf '%s\\n' \"$@\" >> \"$CC_TEST_CANCELS\"\n")

    def write_command(self, name, text):
        path = self.bin_dir / name
        path.write_text(text)
        path.chmod(0o755)

    def write_config(self, **settings):
        self.config.update(settings)
        self.environment.update(self.config)
        (self.repo / "cc_script/config.sh").write_text(
            "\n".join(f"export {key}={shlex.quote(value)}" for key, value in self.config.items()) + "\n"
        )

    def submit(self, *arguments, parallel="2"):
        return subprocess.run(
            ["bash", "cc_script/run.sh", "bp_be", parallel, *arguments],
            cwd=self.repo, env=self.environment, text=True, capture_output=True,
        )

    def submission_calls(self):
        return [json.loads(line) for line in (self.base / "sbatch.jsonl").read_text().splitlines()]

    def test_dry_run_and_invalid_parallelism_do_not_submit(self):
        preview = self.submit("--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("--array=0-1%2", preview.stdout)
        self.assertFalse((self.case_dir / "cc_runs").exists())
        self.assertFalse((self.base / "sbatch.jsonl").exists())
        rejected = self.submit(parallel="0")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse((self.base / "sbatch.jsonl").exists())

    def test_submission_queues_only_array_and_snapshots_prepared_inputs(self):
        self.write_config(CC_CPUS="6", CC_STD_CELL_TIER="upper")
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.submission_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--array=0-1%2", calls[0])
        self.assertFalse(any(arg.startswith("--dependency") for arg in calls[0]))
        self.assertNotIn("prepare", calls[0])
        self.assertTrue(all("--cpus-per-task=6" in call for call in calls))
        run_dir, = (self.case_dir / "cc_runs").iterdir()
        self.assertFalse((self.case_dir / "candidates.json").exists())
        with tarfile.open(run_dir / "inputs.tar") as snapshot:
            saved_seed = snapshot.extractfile("preparation/results/1_synth.sdc").read()
            self.assertEqual(saved_seed, b"seed: 1_synth.sdc\n")
            saved_partition = snapshot.extractfile("solutions/s0000/macro_partition.txt").read()
            self.assertEqual(saved_partition, b"ram_a 1\nram_b 0\n")
        self.assertEqual((run_dir / "solutions.txt").read_text(), "s0000\ns0001\n")
        metadata = json.loads((run_dir / "submission.json").read_text())
        self.assertEqual(metadata["jobs"], {"array": "1001"})
        self.assertEqual([row["solution_id"] for row in metadata["solutions"]], ["s0000", "s0001"])
        self.assertEqual(metadata["solutions"][0]["source_key"], "key\twith\nseparators")

    def test_hardcoded_config_overrides_stale_shell_settings(self):
        self.environment.update(CC_ACCOUNT="aip-stale", CC_CPUS="999", TAIWEI_BASE="/wrong/repository")
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.submission_calls()
        self.assertTrue(all("--account=aip-test" in call for call in calls))
        self.assertTrue(all("--cpus-per-task=8" in call for call in calls))

    def test_empty_account_uses_scheduler_default(self):
        self.write_config(CC_ACCOUNT="")
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.submission_calls():
            self.assertFalse(any(arg.startswith("--account") for arg in call))

    def test_account_setting_can_be_omitted(self):
        self.config.pop("CC_ACCOUNT")
        self.environment.pop("CC_ACCOUNT")
        self.write_config()
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.submission_calls():
            self.assertFalse(any(arg.startswith("--account") for arg in call))

    def test_explicit_paths_need_no_shared_parent_or_fixed_repo_name(self):
        new_repo = self.base / "separate code" / "custom flow"
        new_repo.parent.mkdir()
        self.repo.rename(new_repo)
        self.repo = new_repo
        self.case_dir = self.repo / "partition_experiments/nangate45_3D/bp_be"
        self.seed = self.case_dir / "preparation/results"
        new_orfs = self.base / "different-tools"
        (self.base / "ORFS-Research").rename(new_orfs)
        new_image = self.base / "custom-image.sif"
        (self.base / "containers/orfs.sif").rename(new_image)
        self.write_config(TAIWEI_BASE=str(new_repo), ORFS_BASE=str(new_orfs), CC_IMAGE=str(new_image))
        run_dir = self.ready_fixture()
        worker = self.run_job(0)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertTrue((run_dir / "results/s0000/r0/openroad_eval.json").is_file())
        calls = [json.loads(line) for line in (self.base / "apptainer.jsonl").read_text().splitlines()]
        self.assertTrue(all(str(new_image) in call["args"] for call in calls))

    def test_failed_array_submission_does_not_cancel_other_jobs(self):
        self.environment["CC_TEST_FAIL_ARRAY"] = "1"
        result = self.submit()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / "cancelled.txt").exists())

    def test_metadata_write_failure_cancels_only_the_new_array(self):
        self.environment["CC_TEST_FAIL_METADATA"] = "1"
        result = self.submit()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.base / "cancelled.txt").read_text(), "1001\n")

    def test_missing_ready_seed_fails_before_submitting(self):
        (self.seed / "1_synth.sdc").unlink()
        result = self.submit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("generate_macro_partitions.py", result.stderr)
        self.assertFalse((self.base / "sbatch.jsonl").exists())
        self.assertFalse((self.case_dir / "cc_runs").exists())

    def test_sparse_prepared_ids_are_preserved(self):
        original = self.case_dir / "solutions/s0001"
        renamed = self.case_dir / "solutions/s0007"
        original.rename(renamed)
        info_path = renamed / "solution.json"
        info = json.loads(info_path.read_text())
        info["solution_id"] = "s0007"
        info_path.write_text(json.dumps(info))
        run_dir = self.ready_fixture()
        self.assertEqual((run_dir / "solutions.txt").read_text(), "s0000\ns0007\n")
        result = self.run_job(1)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = run_dir / "results/s0007/r0"
        self.assertIn("solution=s0007", (saved / "status.txt").read_text())
        self.assertTrue((saved / "openroad_eval.json").is_file())

    def test_prepared_solution_metadata_is_optional(self):
        (self.case_dir / "solutions/s0000/solution.json").unlink()
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir, = (self.case_dir / "cc_runs").iterdir()
        metadata = json.loads((run_dir / "submission.json").read_text())
        self.assertEqual(metadata["solutions"][0]["solution_id"], "s0000")
        self.assertIsNone(metadata["solutions"][0]["cost"])

    def make_worker_fixture(self):
        launcher = self.repo / "test/openroad/ORD_3D_NEW_FLOW.sh"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("""#!/bin/bash
set -euo pipefail
[[ "$SKIP_2D_PART" == 1 && "$PIN3D_VIEW_IMPL" == tcl ]]
[[ ! -v START_FROM && ! -v REUSE_2DPART_FROM_VARIANT ]]
directory="reports/$1/$4/$2"
mkdir -p "$directory"
printf '%s\\t%s\\t%s\\n' "$MACRO_PARTITION_FILE" "$STD_CELL_TIER" "$OUTER_ITERATIONS" > "$directory/launcher.tsv"
printf 'mock flow in %s\\n' "$PWD"
if [[ "${CC_TEST_FLOW_SLEEP:-0}" == 1 ]]; then touch "$CC_TEST_STARTED"; exec sleep 60; fi
if [[ "${CC_TEST_FLOW_EXIT:-0}" != 0 ]]; then exit "$CC_TEST_FLOW_EXIT"; fi
if [[ "${CC_TEST_NO_METRICS:-0}" != 1 ]]; then printf '{}\\n' > "$directory/openroad_eval.json"; fi
""")
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        run_path = next(line.removeprefix("Run directory: ") for line in result.stdout.splitlines()
                        if line.startswith("Run directory: "))
        self.run_dir = Path(run_path)
        return self.run_dir

    def job_command(self, index=0, restart=0):
        scratch = self.base / "node-local" / f"task_{index}_r{restart}"
        scratch.mkdir(parents=True, exist_ok=True)
        environment = {
            **self.environment, "SLURM_JOB_ID": str(2001 + index), "SLURM_CPUS_PER_TASK": "6",
            "SLURM_TMPDIR": str(scratch), "SLURM_ARRAY_JOB_ID": "2000",
            "SLURM_ARRAY_TASK_ID": str(index), "SLURM_RESTART_COUNT": str(restart),
            "START_FROM": "ord-route", "REUSE_2DPART_FROM_VARIANT": "wrong",
        }
        command = ["bash", str(self.repo / "cc_script/lib/job.sbatch"), "nangate45_3D", "bp_be",
                   str(self.run_dir.relative_to(self.repo)), "upper", "2"]
        return command, environment

    def run_job(self, index=0, restart=0):
        command, environment = self.job_command(index, restart)
        return subprocess.run(command, env=environment, text=True, capture_output=True, timeout=30)

    def ready_fixture(self):
        run_dir = self.make_worker_fixture()
        self.assertTrue((run_dir / "inputs.tar").is_file())
        self.assertTrue((run_dir / "solutions.txt").is_file())
        return run_dir

    def test_workers_isolate_outputs_reuse_seed_and_refuse_overwrite(self):
        run_dir = self.ready_fixture()
        # Queued tasks must use the input snapshot, even if original inputs change.
        (self.seed / "1_synth.sdc").write_text("changed after submission\n")
        (self.case_dir / "solutions/s0000/macro_partition.txt").write_text("changed after submission\n")
        processes = []
        for index in range(2):
            command, environment = self.job_command(index)
            processes.append(subprocess.Popen(command, env=environment, text=True,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for index, process in enumerate(processes):
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr + stdout)
            variant = f"cc_2000_s{index:04d}_r0"
            saved = run_dir / f"results/s{index:04d}/r0"
            self.assertEqual(json.loads((saved / "openroad_eval.json").read_text()), {})
            with tarfile.open(saved / "output.tar") as archive:
                seed = archive.extractfile(f"results/nangate45_3D/bp_be/{variant}/1_synth.sdc").read()
                self.assertEqual(seed, b"seed: 1_synth.sdc\n")
                partition = archive.extractfile(f"results/nangate45_3D/bp_be/{variant}/partition.txt").read()
                self.assertIn(f"ram_a {1-index}".encode(), partition)
            status = saved / "status.txt"
            previous_status = status.read_text()
            self.assertIn("state=COMPLETED\nexit_code=0", previous_status)
            self.assertIn(str(self.base / "node-local"), previous_status)
            duplicate = self.run_job(index)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual(status.read_text(), previous_status)
        self.assertFalse((self.repo / "results").exists())
        self.assertFalse((self.repo / "reports").exists())
        calls = [json.loads(line) for line in (self.base / "apptainer.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({row["repo"] for row in calls}), 2)
        self.assertTrue(all("NUM_CORES=6" in row["args"] for row in calls))
        self.assertTrue(all("build" not in row["args"] for row in calls))

    def test_worker_failure_is_propagated_and_missing_metrics_are_not_success(self):
        run_dir = self.ready_fixture()
        self.environment["CC_TEST_FLOW_EXIT"] = "37"
        failed = self.run_job(0)
        self.assertEqual(failed.returncode, 37, failed.stderr)
        saved = run_dir / "results/s0000/r0"
        self.assertIn("state=FAILED\nexit_code=37", (saved / "status.txt").read_text())
        self.assertTrue((saved / "output.tar").is_file())
        self.assertIn("mock flow", (saved / "run.log").read_text())
        self.environment["CC_TEST_FLOW_EXIT"] = "0"
        self.environment["CC_TEST_NO_METRICS"] = "1"
        missing = self.run_job(1)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("state=FAILED\nexit_code=1", (run_dir / "results/s0001/r0/status.txt").read_text())

    def test_requeue_uses_a_separate_saved_attempt(self):
        run_dir = self.ready_fixture()
        first = self.run_job(0)
        self.assertEqual(first.returncode, 0, first.stderr)
        restarted = self.run_job(0, restart=1)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertTrue((run_dir / "results/s0000/r0/output.tar").is_file())
        self.assertTrue((run_dir / "results/s0000/r1/output.tar").is_file())

    def test_missing_node_local_storage_has_no_shared_work_fallback(self):
        self.make_worker_fixture()
        command, environment = self.job_command()
        environment.pop("SLURM_TMPDIR")
        result = subprocess.run(command, env=environment, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Node-local storage is required", result.stderr)
        self.assertFalse((self.base / "apptainer.jsonl").exists())

    def test_failed_copy_back_cannot_be_reported_as_completed(self):
        run_dir = self.ready_fixture()
        tar = shutil.which("tar")
        self.write_command("tar", f"""#!/bin/bash
for arg in "$@"; do
  if [[ "$arg" == */output.tar.partial ]]; then exit 9; fi
done
exec '{tar}' "$@"
""")
        result = self.run_job(0)
        self.assertNotEqual(result.returncode, 0)
        status = (run_dir / "results/s0000/r0/status.txt").read_text()
        self.assertIn("state=FAILED\nexit_code=1", status)

    def test_advance_timeout_signal_saves_partial_outputs(self):
        run_dir = self.ready_fixture()
        self.environment["CC_TEST_FLOW_SLEEP"] = "1"
        command, environment = self.job_command(0)
        process = subprocess.Popen(command, env=environment, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not (self.base / "flow-started").exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.base / "flow-started").exists())
            process.send_signal(signal.SIGUSR1)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 143, stdout + stderr)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)
        saved = run_dir / "results/s0000/r0"
        self.assertTrue((saved / "output.tar").is_file())
        self.assertIn("state=INTERRUPTED\nexit_code=143", (saved / "status.txt").read_text())
        self.assertIn("interrupted=TIME_LIMIT_WARNING", (saved / "status.txt").read_text())


if __name__ == "__main__":
    unittest.main()
