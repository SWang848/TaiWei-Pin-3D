#!/usr/bin/env python3
"""Submit prepared partitions; save each run below SCRATCH/taiwei."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


SEED_FILES = ("1_synth.sdc", "2_2_floorplan_io.def", "2_2_floorplan_io.v")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise ValueError("must be a positive integer")
    return number


def read_prepared_inputs(input_dir, limit):
    """Validate the shared seed and select the existing solution IDs."""
    for name in SEED_FILES:
        path = input_dir / "preparation/results" / name
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Missing prepared seed: {path}. Run generate_macro_partitions.py first.")

    partitions = sorted((input_dir / "solutions").glob("s[0-9]*/macro_partition.txt"))
    if limit is not None:
        partitions = partitions[:limit]
    if not partitions:
        raise ValueError(f"No prepared partitions in {input_dir / 'solutions'}")

    solutions = []
    for partition in partitions:
        solution_id = partition.parent.name
        if not re.fullmatch(r"s[0-9]+", solution_id) or partition.stat().st_size == 0:
            raise ValueError(f"Invalid prepared partition: {partition}")
        info_path = partition.with_name("solution.json")
        info = json.loads(info_path.read_text()) if info_path.is_file() else {}
        if not isinstance(info, dict) or info.get("solution_id", solution_id) != solution_id:
            raise ValueError(f"Solution identity does not match {partition}")
        solutions.append({"solution_id": solution_id, "source_key": info.get("source_key"),
                          "cost": info.get("cost")})
    return solutions


def create_run(output_root, solutions):
    """Create only logs/ and results/ under a unique scratch run directory."""
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_")
    run_dir = Path(tempfile.mkdtemp(prefix=stamp, dir=output_root))
    (run_dir / "logs").mkdir()
    (run_dir / "results").mkdir()
    (run_dir / "logs/solutions.txt").write_text(
        "".join(item["solution_id"] + "\n" for item in solutions)
    )
    return run_dir


def build_submission(repo, run_dir, platform, case, parallel, count, resources, tier, iterations, account):
    command = [
        "sbatch", "--parsable", "--export=ALL", "--nodes=1", "--ntasks=1",
        f"--cpus-per-task={resources['cpus_per_task']}", f"--mem={resources['memory']}",
        f"--time={resources['time']}", f"--chdir={run_dir}", "--signal=B:USR1@300",
        "--no-requeue", f"--array=0-{count - 1}%{parallel}",
        f"--output={run_dir / 'logs/partition-%A_%a.out'}",
    ]
    if account:
        command.append(f"--account={account}")
    command += [str(repo / "cc_script/lib/job.sbatch"), platform, case, str(run_dir),
                tier, str(iterations)]
    return command


def save_submission(run_dir, metadata):
    destination = run_dir / "logs/submission.json"
    temporary = destination.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(metadata, indent=2) + "\n")
        temporary.replace(destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", nargs="?", default="bp_be")
    parser.add_argument("parallel", nargs="?", type=positive_int, default=4)
    parser.add_argument("--limit", type=positive_int,
                        help="run at most this many partitions, in solution-ID order")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    env = os.environ

    repo = Path(env["TAIWEI_BASE"]).resolve()
    input_root = Path(env.get("CC_INPUT_ROOT", repo / "partition_experiments")).resolve()
    scratch = Path(env["SCRATCH"]).resolve()
    output_root = Path(env["CC_OUTPUT_ROOT"]).resolve()
    if not output_root.is_relative_to(scratch) or output_root == scratch:
        raise ValueError("CC_OUTPUT_ROOT must be a subdirectory of SCRATCH")
    platform = env["CC_PLATFORM"]
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in (platform, args.case)):
        raise ValueError("Platform and case must be names without slashes")
    tier = env["CC_STD_CELL_TIER"]
    if tier not in ("bottom", "upper"):
        raise ValueError("CC_STD_CELL_TIER must be bottom or upper")
    iterations = positive_int(env["CC_OUTER_ITERATIONS"])
    resources = {"cpus_per_task": positive_int(env["CC_CPUS"]),
                 "memory": env["CC_MEMORY"].strip(), "time": env["CC_TIME"].strip()}
    if not resources["memory"] or not resources["time"]:
        raise ValueError("CC_MEMORY and CC_TIME must be set")

    input_dir = input_root / platform / args.case
    solutions = read_prepared_inputs(input_dir, args.limit)
    run_dir = output_root / "RUN_ID"
    if not args.dry_run:
        runtime_paths = (Path(env["CC_IMAGE"]), Path(env["ORFS_BASE"]) / "tools/install",
                         Path(env["ORFS_BASE"]) / "dependencies/lib")
        for path in runtime_paths:
            if not path.exists():
                raise FileNotFoundError(f"Missing runtime input: {path}")
        run_dir = create_run(output_root, solutions)

    command = build_submission(repo, run_dir, platform, args.case, args.parallel,
                               len(solutions), resources, tier, iterations,
                               env.get("CC_ACCOUNT", "").strip())
    print(f"Prepared solutions: {len(solutions)}; maximum concurrent candidates: {args.parallel}")
    if args.limit is not None:
        print("Selected solutions: " + ", ".join(item["solution_id"] for item in solutions))
    print(f"Input directory: {input_dir}")
    print(f"Run directory: {run_dir}")
    if args.dry_run:
        print(shlex.join(command))
        return

    job_id = subprocess.check_output(command, text=True).strip().split(";")[0]
    if not job_id.isdigit():
        raise ValueError(f"Unexpected Slurm job ID: {job_id!r}")
    metadata = {"input_directory": str(input_dir), "platform": platform, "case": args.case,
                "max_parallel": args.parallel, "solutions": solutions,
                "jobs": {"array": job_id}, "working_storage": "SLURM_TMPDIR",
                "layout_version": 3, "input_mode": "direct-copy",
                "solution_list": "logs/solutions.txt", "resources": resources}
    try:
        save_submission(run_dir, metadata)
    except OSError:
        subprocess.run(["scancel", job_id], check=False)
        raise
    print(f"Candidate array: {job_id}")
    print(f"Monitor: squeue --array --jobs={job_id}")
    print(f"Saved results: {run_dir / 'results'}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
