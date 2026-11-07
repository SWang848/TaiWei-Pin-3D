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
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()
        self.output_root = self.scratch / "taiwei"
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
        library_dir = self.base / "ORFS-Research/dependencies/lib"
        library_dir.mkdir(parents=True)
        (library_dir / "libortools.so.9").write_text("dependency fixture\n")
        tool = self.base / "ORFS-Research/tools/install/OpenROAD/bin/openroad"
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/bash\nprintf 'mock OpenROAD version\\n'\n")
        tool.chmod(0o755)
        for directory in ("platforms", "scripts_openroad", "util", "test"):
            (self.repo / directory).mkdir(exist_ok=True)
        (self.repo / "Makefile").write_text("# fixture\n")
        (self.repo / "env.sh").write_text(":\n")
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir()
        self.environment = {
            **os.environ, "CC_BASE": str(self.base), "CC_ACCOUNT": "aip-test",
            "SCRATCH": str(self.scratch),
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "CC_TEST_CALLS": str(self.base / "sbatch.jsonl"),
            "CC_TEST_CANCELS": str(self.base / "cancelled.txt"),
        }
        # Keep the cluster's Bash startup hooks from replacing the mock module command.
        for name in ("BASH_ENV", "ENV", "BASH_FUNC_module%%", "BASH_FUNC_ml%%"):
            self.environment.pop(name, None)
        self.environment.pop("CC_IMAGE", None)
        self.environment.pop("CC_TEST_FAIL_ARRAY", None)
        self.environment["CC_TEST_APPTAINER_CALLS"] = str(self.base / "apptainer.jsonl")
        self.environment["CC_TEST_STARTED"] = str(self.base / "flow-started")
        self.config = {
            "CC_BASE": str(self.base), "TAIWEI_BASE": str(self.repo),
            "ORFS_BASE": str(self.base / "ORFS-Research"),
            "CC_INPUT_ROOT": str(self.repo / "partition_experiments"),
            "CC_OUTPUT_ROOT": str(self.output_root),
            "CC_IMAGE": str(self.base / "containers/orfs.sif"), "CC_ACCOUNT": "aip-test",
            "CC_PLATFORM": "nangate45_3D", "CC_CPUS": "8", "CC_MEMORY": "32G",
            "CC_TIME": "12:00:00",
            "CC_STD_CELL_TIER": "bottom", "CC_OUTER_ITERATIONS": "1",
        }
        self.write_config()
        self.write_command("module", "#!/bin/bash\nexit 0\n")
        self.write_command("apptainer", f"""#!{sys.executable}
import json, os, pathlib, shlex, signal, subprocess, sys
args = sys.argv[1:]
assert args[0] == 'exec' and 'build' not in args
if os.environ.get('CC_TEST_IGNORE_TERM') == '1':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
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
assert (pathlib.Path(binds['/ORFS-Research']) / 'dependencies/lib/libortools.so.9').read_text() == 'dependency fixture\\n'
with open(os.environ['CC_TEST_APPTAINER_CALLS'], 'a') as handle:
    handle.write(json.dumps({{'repo': str(repo), 'args': args}}) + '\\n')
container_script = repo / 'cc_script/lib/container.sh'
container_script.write_text(container_script.read_text().replace(
    'export ORFS_DIR=/ORFS-Research',
    'export ORFS_DIR=' + shlex.quote(binds['/ORFS-Research'])))
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
    (pathlib.Path(output).parent / 'submission.json').mkdir()
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
        self.assertFalse(self.output_root.exists())
        self.assertIn(str(self.output_root / "RUN_ID"), preview.stdout)
        self.assertFalse((self.base / "sbatch.jsonl").exists())
        rejected = self.submit(parallel="0")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse((self.base / "sbatch.jsonl").exists())

    def test_entry_script_can_run_outside_the_repository(self):
        result = subprocess.run(
            ["bash", str(self.repo / "cc_script/run.sh"), "bp_be", "2", "--dry-run"],
            cwd=self.base, env=self.environment, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.output_root / "RUN_ID"), result.stdout)
        self.assertFalse(self.output_root.exists())

    def test_invalid_settings_do_not_create_a_run_or_submit(self):
        valid = {"CC_CPUS": "8", "CC_OUTER_ITERATIONS": "1", "CC_STD_CELL_TIER": "bottom",
                 "CC_MEMORY": "32G", "CC_TIME": "12:00:00"}
        for invalid in ({"CC_CPUS": "0"}, {"CC_OUTER_ITERATIONS": "0"},
                        {"CC_STD_CELL_TIER": "sideways"}, {"CC_MEMORY": ""}, {"CC_TIME": ""}):
            with self.subTest(invalid=invalid):
                self.write_config(**{**valid, **invalid})
                result = self.submit()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output_root.exists())
                self.assertFalse((self.base / "sbatch.jsonl").exists())

    def test_limit_keeps_only_the_selected_partitions_in_the_run(self):
        result = self.submit("--limit", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--array=0-0%2", self.submission_calls()[0])
        run_dir, = self.output_root.iterdir()
        self.assertEqual((run_dir / "logs/solutions.txt").read_text(), "s0000\n")
        metadata = json.loads((run_dir / "logs/submission.json").read_text())
        self.assertEqual([row["solution_id"] for row in metadata["solutions"]], ["s0000"])

    def test_prepared_input_root_can_live_outside_the_code_directory(self):
        input_root = self.base / "prepared inputs"
        new_case = input_root / "nangate45_3D/bp_be"
        new_case.parent.mkdir(parents=True)
        self.case_dir.rename(new_case)
        self.case_dir = new_case
        self.seed = new_case / "preparation/results"
        self.write_config(CC_INPUT_ROOT=str(input_root))
        run_dir = self.ready_fixture()
        worker = self.run_job(0)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertTrue((run_dir / "results/s0000/openroad_eval.json").is_file())

    def test_submission_queues_only_array_and_saves_outputs_on_scratch(self):
        self.write_config(CC_CPUS="6", CC_STD_CELL_TIER="upper")
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.submission_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--array=0-1%2", calls[0])
        self.assertIn("--no-requeue", calls[0])
        self.assertFalse(any(arg.startswith("--dependency") for arg in calls[0]))
        self.assertNotIn("prepare", calls[0])
        self.assertTrue(all("--cpus-per-task=6" in call for call in calls))
        run_dir, = self.output_root.iterdir()
        self.assertFalse((self.case_dir / "candidates.json").exists())
        self.assertFalse((self.case_dir / "cc_runs").exists())
        self.assertEqual(sorted(path.name for path in run_dir.iterdir()), ["logs", "results"])
        self.assertEqual(sorted(path.name for path in (run_dir / "logs").iterdir()),
                         ["solutions.txt", "submission.json"])
        self.assertIn(f"--chdir={run_dir}", calls[0])
        self.assertIn(str(run_dir), calls[0])
        self.assertEqual((run_dir / "logs/solutions.txt").read_text(), "s0000\ns0001\n")
        metadata = json.loads((run_dir / "logs/submission.json").read_text())
        self.assertEqual(metadata["jobs"], {"array": "1001"})
        self.assertEqual(metadata["layout_version"], 3)
        self.assertEqual(metadata["input_mode"], "direct-copy")
        self.assertEqual(metadata["input_directory"], str(self.case_dir))
        self.assertEqual(metadata["resources"], {
            "cpus_per_task": 6, "memory": "32G", "time": "12:00:00",
        })
        self.assertEqual([row["solution_id"] for row in metadata["solutions"]], ["s0000", "s0001"])
        self.assertEqual(metadata["solutions"][0]["source_key"], "key\twith\nseparators")

    def test_hardcoded_config_overrides_stale_shell_settings(self):
        self.environment.update(CC_ACCOUNT="aip-stale", CC_CPUS="999", TAIWEI_BASE="/wrong/repository")
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.submission_calls()
        self.assertTrue(all("--account=aip-test" in call for call in calls))
        self.assertTrue(all("--cpus-per-task=8" in call for call in calls))

    def test_output_root_must_be_below_scratch(self):
        for location in (self.repo / "outputs", self.scratch):
            with self.subTest(location=location):
                self.write_config(CC_OUTPUT_ROOT=str(location))
                result = self.submit()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CC_OUTPUT_ROOT must be a subdirectory of SCRATCH", result.stderr)
                self.assertFalse((self.base / "sbatch.jsonl").exists())

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
        self.write_config(TAIWEI_BASE=str(new_repo), CC_INPUT_ROOT=str(new_repo / "partition_experiments"),
                          ORFS_BASE=str(new_orfs), CC_IMAGE=str(new_image))
        run_dir = self.ready_fixture()
        worker = self.run_job(0)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertTrue((run_dir / "results/s0000/openroad_eval.json").is_file())
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
        run_dir, = self.output_root.iterdir()
        self.assertFalse((run_dir / "logs/submission.json.tmp").exists())

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
        self.assertEqual((run_dir / "logs/solutions.txt").read_text(), "s0000\ns0007\n")
        result = self.run_job(1)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = run_dir / "results/s0007"
        self.assertIn("s0007: COMPLETED (exit 0)", result.stdout)
        self.assertTrue((saved / "openroad_eval.json").is_file())

    def test_prepared_solution_metadata_is_optional(self):
        (self.case_dir / "solutions/s0000/solution.json").unlink()
        result = self.submit()
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir, = self.output_root.iterdir()
        metadata = json.loads((run_dir / "logs/submission.json").read_text())
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
                   str(self.run_dir), "upper", "2"]
        return command, environment

    def run_job(self, index=0, restart=0):
        command, environment = self.job_command(index, restart)
        return subprocess.run(command, env=environment, text=True, capture_output=True, timeout=30)

    def ready_fixture(self):
        run_dir = self.make_worker_fixture()
        self.assertTrue((run_dir / "logs/solutions.txt").is_file())
        self.assertEqual(run_dir.parent, self.output_root)
        return run_dir

    def test_workers_isolate_outputs_copy_inputs_and_refuse_overwrite(self):
        run_dir = self.ready_fixture()
        # Inputs are copied directly when the worker starts, without an archive.
        (self.seed / "1_synth.sdc").write_text("changed after submission\n")
        processes = []
        for index in range(2):
            command, environment = self.job_command(index)
            processes.append(subprocess.Popen(command, env=environment, text=True,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for index, process in enumerate(processes):
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr + stdout)
            variant = f"cc_2000_s{index:04d}"
            saved = run_dir / f"results/s{index:04d}"
            self.assertEqual(json.loads((saved / "openroad_eval.json").read_text()), {})
            with tarfile.open(saved / "output.tar") as archive:
                seed = archive.extractfile(f"results/nangate45_3D/bp_be/{variant}/1_synth.sdc").read()
                self.assertEqual(seed, b"changed after submission\n")
                partition = archive.extractfile(f"results/nangate45_3D/bp_be/{variant}/partition.txt").read()
                self.assertIn(f"ram_a {1-index}".encode(), partition)
                self.assertIn(b"mock flow", archive.extractfile("run.log").read())
            self.assertIn("COMPLETED (exit 0)", stdout)
            self.assertEqual(sorted(path.name for path in saved.iterdir()),
                             ["openroad_eval.json", "output.tar"])
            previous_archive = (saved / "output.tar").read_bytes()
            duplicate = self.run_job(index)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual((saved / "output.tar").read_bytes(), previous_archive)
        self.assertFalse((self.repo / "results").exists())
        self.assertFalse((self.repo / "reports").exists())
        calls = [json.loads(line) for line in (self.base / "apptainer.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({row["repo"] for row in calls}), 2)
        self.assertTrue(all("NUM_CORES=6" in row["args"] for row in calls))
        self.assertTrue(all("build" not in row["args"] for row in calls))

    def test_runtime_failure_before_flow_keeps_an_archived_diagnostic(self):
        run_dir = self.ready_fixture()
        self.write_command("apptainer", "#!/bin/bash\nprintf 'runtime failure\\n' >&2\nexit 127\n")
        failed = self.run_job(0)
        self.assertEqual(failed.returncode, 127, failed.stderr)
        self.assertIn("FAILED (exit 127)", failed.stdout)
        saved = run_dir / "results/s0000"
        with tarfile.open(saved / "output.tar") as archive:
            self.assertEqual(archive.getnames(), ["run.log"])
            self.assertIn(b"runtime failure", archive.extractfile("run.log").read())

    def test_worker_failure_is_propagated_and_missing_metrics_are_not_success(self):
        run_dir = self.ready_fixture()
        self.environment["CC_TEST_FLOW_EXIT"] = "37"
        failed = self.run_job(0)
        self.assertEqual(failed.returncode, 37, failed.stderr)
        saved = run_dir / "results/s0000"
        self.assertIn("FAILED (exit 37)", failed.stdout)
        self.assertTrue((saved / "output.tar").is_file())
        with tarfile.open(saved / "output.tar") as archive:
            self.assertIn(b"mock flow", archive.extractfile("run.log").read())
        self.environment["CC_TEST_FLOW_EXIT"] = "0"
        self.environment["CC_TEST_NO_METRICS"] = "1"
        missing = self.run_job(1)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("FAILED (exit 1)", missing.stdout)

    def test_requeue_cannot_overwrite_a_saved_solution(self):
        run_dir = self.ready_fixture()
        first = self.run_job(0)
        self.assertEqual(first.returncode, 0, first.stderr)
        saved = run_dir / "results/s0000/output.tar"
        previous_archive = saved.read_bytes()
        restarted = self.run_job(0, restart=1)
        self.assertNotEqual(restarted.returncode, 0)
        self.assertEqual(saved.read_bytes(), previous_archive)

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
        self.assertIn("FAILED (exit 1)", result.stdout)

    def test_advance_timeout_signal_saves_partial_outputs(self):
        run_dir = self.ready_fixture()
        self.environment["CC_TEST_FLOW_SLEEP"] = "1"
        self.environment["CC_TEST_IGNORE_TERM"] = "1"
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
        saved = run_dir / "results/s0000"
        self.assertTrue((saved / "output.tar").is_file())
        self.assertIn("INTERRUPTED (exit 143)", stdout)
        self.assertIn("signal=TIME_LIMIT_WARNING", stdout)


if __name__ == "__main__":
    unittest.main()
