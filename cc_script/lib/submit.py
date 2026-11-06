#!/usr/bin/env python3
"""Run already-prepared partitions as a parallel Slurm array."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile


def positive_int(value):
    number = int(value)
    if number < 1:
        raise ValueError("must be a positive integer")
    return number


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
    platform = env["CC_PLATFORM"]
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in (platform, args.case)):
        raise ValueError("Platform and case must be names without slashes")
    case_dir = repo / "partition_experiments" / platform / args.case
    seed = case_dir / "preparation/results"
    seed_files = ("1_synth.sdc", "2_2_floorplan_io.def", "2_2_floorplan_io.v")
    for name in seed_files:
        if not (seed / name).is_file() or (seed / name).stat().st_size == 0:
            raise FileNotFoundError(f"Missing prepared seed: {seed / name}. Run generate_macro_partitions.py first.")
    partitions = sorted((case_dir / "solutions").glob("s[0-9]*/macro_partition.txt"))
    if not partitions:
        raise ValueError(f"No prepared partitions in {case_dir / 'solutions'}")
    if args.limit is not None:
        partitions = partitions[:args.limit]
    solutions = []
    for partition in partitions:
        solution_id = partition.parent.name
        if not re.fullmatch(r"s[0-9]+", solution_id) or partition.stat().st_size == 0:
            raise ValueError(f"Invalid prepared partition: {partition}")
        info_file = partition.with_name("solution.json")
        info = json.loads(info_file.read_text()) if info_file.is_file() else {}
        if not isinstance(info, dict) or info.get("solution_id", solution_id) != solution_id:
            raise ValueError(f"Solution identity does not match {partition}")
        solutions.append({"solution_id": solution_id, "source_key": info.get("source_key"), "cost": info.get("cost")})

    # Keep one private input snapshot so queued jobs use the same prepared files.
    run_dir = case_dir / "cc_runs/RUN_ID"
    if not args.dry_run:
        for path in (Path(env["CC_IMAGE"]), Path(env["ORFS_BASE"]) / "tools/install",
                     Path(env["ORFS_BASE"]) / "dependencies/lib"):
            if not path.exists():
                raise FileNotFoundError(f"Missing runtime input: {path}")
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_")
        run_dir = Path(tempfile.mkdtemp(prefix=stamp, dir=run_dir.parent))
        (run_dir / "slurm").mkdir()
        (run_dir / "solutions.txt").write_text("".join(item["solution_id"] + "\n" for item in solutions))
        with tarfile.open(run_dir / "inputs.tar", "w", dereference=True) as archive:
            archive.add(seed, arcname="preparation/results", recursive=False)
            for name in seed_files:
                archive.add(seed / name, arcname=f"preparation/results/{name}")
            for partition in partitions:
                archive.add(partition.parent, arcname=f"solutions/{partition.parent.name}", recursive=False)
                for name in ("macro_partition.txt", "solution.json"):
                    path = partition.parent / name
                    if path.is_file():
                        archive.add(path, arcname=f"solutions/{partition.parent.name}/{name}")

    command = [
        "sbatch", "--parsable", "--export=ALL", "--nodes=1", "--ntasks=1",
        f"--cpus-per-task={positive_int(env['CC_CPUS'])}", f"--mem={env['CC_MEMORY']}",
        f"--time={env['CC_TIME']}", f"--chdir={repo}", "--signal=B:USR1@300",
        f"--array=0-{len(solutions) - 1}%{args.parallel}",
        f"--output={run_dir / 'slurm/partition-%A_%a.out'}",
    ]
    if env.get("CC_ACCOUNT", "").strip():
        command.append(f"--account={env['CC_ACCOUNT'].strip()}")
    command += [str(repo / "cc_script/lib/job.sbatch"), platform, args.case,
                str(run_dir.relative_to(repo)), env["CC_STD_CELL_TIER"],
                str(positive_int(env["CC_OUTER_ITERATIONS"]))]
    print(f"Prepared solutions: {len(solutions)}; maximum concurrent candidates: {args.parallel}")
    if args.limit is not None:
        print("Selected solutions: " + ", ".join(item["solution_id"] for item in solutions))
    print(f"Run directory: {run_dir}")
    if args.dry_run:
        print(shlex.join(command))
        return

    job_id = subprocess.check_output(command, text=True).strip().split(";")[0]
    if not job_id.isdigit():
        raise ValueError(f"Unexpected Slurm job ID: {job_id!r}")
    metadata = {"input_directory": str(case_dir), "platform": platform, "case": args.case,
                "max_parallel": args.parallel, "solutions": solutions,
                "jobs": {"array": job_id}, "working_storage": "SLURM_TMPDIR"}
    try:
        (run_dir / "submission.json").write_text(json.dumps(metadata, indent=2) + "\n")
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
