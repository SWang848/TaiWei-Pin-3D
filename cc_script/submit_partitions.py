#!/usr/bin/env python3
"""Submit one seed preparation job followed by parallel 3D candidate jobs."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from generate_macro_partitions import read_candidate_records


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, help="Design nickname, e.g. bp_be or bp_fe")
    parser.add_argument("--platform", default="nangate45_3D")
    parser.add_argument("--account", default=os.environ.get("CC_ACCOUNT"))
    parser.add_argument("--candidates", type=Path, help="Alternate candidate JSON to snapshot")
    parser.add_argument("--max-parallel", type=positive_int, default=4)
    parser.add_argument("--cpus-per-task", type=positive_int, default=8)
    parser.add_argument("--mem", default="32G", help="Memory per array task and preparation job")
    parser.add_argument("--time", default="12:00:00", help="Time limit per 3D candidate")
    parser.add_argument("--prepare-time", default="02:00:00")
    parser.add_argument("--std-cell-tier", choices=("bottom", "upper"), default="bottom")
    parser.add_argument("--outer-iterations", type=positive_int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="Show commands without creating runs or submitting jobs")
    return parser.parse_args(argv)


def submit_job(command, environment):
    result = subprocess.run(command, env=environment, text=True, capture_output=True, check=True)
    job_id = result.stdout.strip().split(";")[0]
    if not job_id.isdigit():
        raise ValueError(f"Unexpected sbatch --parsable output: {result.stdout!r}")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return job_id


def main(argv=None):
    args = parse_args(argv)
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", item) for item in (args.platform, args.case)):
        raise ValueError("Platform and case must be directory names without slashes")
    case_dir = REPO_ROOT / "partition_experiments" / args.platform / args.case
    for config in ("config2d.mk", "config.mk"):
        path = REPO_ROOT / "designs" / args.platform / args.case / config
        if not path.is_file():
            raise ValueError(f"Missing design configuration: {path}")
    candidates = (args.candidates or case_dir / "candidates.json").resolve()
    records = read_candidate_records(candidates)

    environment = os.environ.copy()
    if not args.dry_run:
        if not args.account:
            raise ValueError("Set --account or export CC_ACCOUNT (your Vulcan aip- account)")
        if not environment.get("CC_BASE"):
            raise ValueError("Export CC_BASE to the directory used for the upload")
        base = Path(environment["CC_BASE"]).expanduser().resolve()
        if (base / "TaiWei-Pin-3D").resolve() != REPO_ROOT:
            raise ValueError("Run this uploaded script from $CC_BASE/TaiWei-Pin-3D on the cluster")
        environment["CC_BASE"] = str(base)
        image = Path(environment.get("CC_IMAGE", str(base / "containers" / "orfs.sif"))).expanduser().resolve()
        if not image.is_file():
            raise ValueError(f"Missing prebuilt SIF: {image}")
        environment["CC_IMAGE"] = str(image)
        if not (base / "ORFS-Research" / "tools" / "install").is_dir():
            raise ValueError("Missing $CC_BASE/ORFS-Research/tools/install")
        if not shutil.which("sbatch"):
            raise ValueError("sbatch is unavailable; submit on the cluster login node")

    if args.dry_run:
        run_dir = case_dir / "cc_runs" / "RUN_ID"
    else:
        runs_dir = case_dir / "cc_runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        prefix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_")
        run_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=runs_dir))
        shutil.copyfile(candidates, run_dir / "candidates.json")
        records = read_candidate_records(run_dir / "candidates.json")
        aliases = candidates.parent / "macro_name_map.tsv"
        if aliases.is_file():
            shutil.copyfile(aliases, run_dir / aliases.name)
        (run_dir / "slurm").mkdir()

    relative_run = str(run_dir.relative_to(REPO_ROOT))
    job_args = [args.platform, args.case, relative_run, args.std_cell_tier, str(args.outer_iterations)]
    common = [
        "sbatch", "--parsable", "--export=ALL", f"--account={args.account or 'YOUR_ACCOUNT'}",
        "--nodes=1", "--ntasks=1", f"--cpus-per-task={args.cpus_per_task}",
        f"--mem={args.mem}", f"--chdir={REPO_ROOT}",
    ]
    prepare = common + [
        f"--time={args.prepare_time}", f"--output={run_dir / 'slurm' / 'prepare-%j.out'}",
        str(REPO_ROOT / "cc_script" / "prepare_partitions.sbatch"), *job_args,
    ]

    def array_command(dependency):
        return common + [
            f"--time={args.time}", f"--array=0-{len(records) - 1}%{args.max_parallel}",
            f"--dependency=afterok:{dependency}", "--kill-on-invalid-dep=yes",
            f"--output={run_dir / 'slurm' / 'partition-%A_%a.out'}",
            str(REPO_ROOT / "cc_script" / "run_partitions.sbatch"), *job_args,
        ]

    print(f"Solutions: {len(records)}; maximum concurrent candidates: {args.max_parallel}")
    print(f"Run directory: {run_dir}")
    if args.dry_run:
        print(shlex.join(prepare))
        print(shlex.join(array_command("PREPARE_JOB_ID")))
        return 0

    metadata = {
        "source_candidates": str(candidates), "platform": args.platform, "case": args.case,
        "max_parallel": args.max_parallel, "cpus_per_task": args.cpus_per_task,
        "mem": args.mem, "time": args.time, "prepare_time": args.prepare_time,
        "std_cell_tier": args.std_cell_tier, "outer_iterations": args.outer_iterations,
        "solutions": [
            {"solution_id": f"s{index:04d}", "source_key": key, "cost": record["cost"]}
            for index, (key, record) in enumerate(records.items())
        ],
        "jobs": {},
    }
    metadata_path = run_dir / "submission.json"

    def save_metadata():
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    save_metadata()
    try:
        metadata["jobs"]["prepare"] = submit_job(prepare, environment)
        save_metadata()
        metadata["jobs"]["array"] = submit_job(array_command(metadata["jobs"]["prepare"]), environment)
        save_metadata()
    except (OSError, ValueError, subprocess.CalledProcessError):
        # Only cancel jobs created by this submission if the chain could not be queued.
        for job_id in metadata["jobs"].values():
            try:
                subprocess.run(["scancel", job_id], env=environment, check=False)
            except OSError as cleanup_error:
                print(f"Could not cancel job {job_id}: {cleanup_error}", file=sys.stderr)
        raise
    print(f"Preparation job: {metadata['jobs']['prepare']}")
    print(f"Candidate array: {metadata['jobs']['array']}")
    print(f"Monitor: squeue --array --jobs={metadata['jobs']['array']}")
    print(f"Slurm logs: {run_dir / 'slurm'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr, end="")
        raise SystemExit(1)
