# Compute Canada / Alliance partition experiments

These scripts evaluate every candidate in a Pareto archive using the full
OpenROAD 3D flow. They reuse the prebuilt `orfs.sif`; they never build a
container or request a GPU for this CPU workload.

## Upload layout

Set `CC_BASE` to the same absolute directory used for the initial upload:

```text
$CC_BASE/
  containers/orfs.sif
  ORFS-Research/tools/install/...
  TaiWei-Pin-3D/cc_script/...
```

The installed OpenROAD and Yosys must already include any compiled source
changes. Use Git or rsync to upload this directory before submitting.

## Submit from the cluster login node

```bash
export CC_BASE="/project/YOUR_ALLOCATION/YOUR_USERNAME/taiwei"
export CC_ACCOUNT="aip-YOUR_GROUP"
cd "$CC_BASE/TaiWei-Pin-3D"

# Preview the job commands without creating files or submitting jobs.
python3 cc_script/submit_partitions.py --case bp_be --max-parallel 4 --dry-run

# Build the seed once, then run up to four candidates concurrently.
python3 cc_script/submit_partitions.py --case bp_be --max-parallel 4

# Another case, with explicit resource limits per job.
python3 cc_script/submit_partitions.py --case bp_fe --max-parallel 8 \
  --cpus-per-task 8 --mem 32G --time 12:00:00
```

The submission helper uses only the Python standard library and performs no
EDA work on the login node. If `python3` is unavailable, load your cluster's
Python module first. No interactive `salloc` session is needed for submission.
`--account` overrides `CC_ACCOUNT`; `CC_IMAGE` overrides the default SIF path.
Resources and time limits are starting defaults, not measured requirements.

## What runs

1. A unique run directory snapshots the candidate JSON and any adjacent name
   aliases. Each submission has its own snapshot, seed, and generated solutions.
2. A preparation job calls [the existing generator](../generate_macro_partitions.py)
   with `--keep-preparation`, synthesizing and floorplanning the case once.
3. A Slurm array waits for preparation to finish successfully. Array index 0
   evaluates `s0000`, index 1 evaluates `s0001`, and so on. The `%N` array limit
   controls the maximum concurrent candidate runs; actual concurrency also
   depends on cluster availability. If preparation fails, the dependent array
   is cancelled instead of remaining pending indefinitely.
4. Each task copies the seed into a private flow variant, imports its macro
   partition, and runs [the maintained OpenROAD launcher](../test/openroad/ORD_3D_NEW_FLOW.sh)
   from `ord-pre` through `ord-final`. It skips repeated synthesis and TritonPart.

Every exported solution is evaluated, including duplicates identified by the
generator. Standard cells use the bottom tier by default; set
`--std-cell-tier upper` to change this. `--outer-iterations N` controls placement
iterations. Candidate inputs are frozen, but the source code and installed
tools are mounted: keep those fixed while comparing jobs from one submission.

## Outputs and monitoring

For a printed run directory such as
`partition_experiments/nangate45_3D/bp_be/cc_runs/<run-id>/`:

- `submission.json`: source keys, costs, resource settings, and Slurm job IDs.
- `manifest.tsv`, `solutions/`, `preparation/`: generator outputs and shared seed.
- `slurm/prepare-<job-id>.out`: preparation output.
- `slurm/partition-<array-id>_<index>.out`: one log per candidate.
- `task_status/`: per-attempt TSV files with the solution, variant, state,
  exit code, and final metrics path. They are written independently, with no
  shared append file. Jobs terminated before their shell can clean up may
  leave a RUNNING entry; Slurm accounting is authoritative in that case.

Each task's variant is `cc_<array-id>_s<index>_r<restart-count>`. Outputs use the
normal repository trees:

```text
results/<platform>/<case>/<variant>/
logs/<platform>/<case>/<variant>/
objects/<platform>/<case>/<variant>/
reports/<platform>/<case>/<variant>/openroad_eval.json
```

```bash
squeue --array --jobs=ARRAY_JOB_ID
sacct -j ARRAY_JOB_ID --format=JobID,State,ExitCode,Elapsed,MaxRSS
```

Use a new submission to rerun a failed experiment. A fresh array ID prevents
overwriting earlier outputs. Slurm requeues also receive a new variant suffix.
The scripts perform no cleanup of previous results and no Cadence evaluation.

## Files

- [submit_partitions.py](submit_partitions.py): validate inputs and submit the dependency chain.
- [prepare_partitions.sbatch](prepare_partitions.sbatch): one shared 2D preparation job.
- [run_partitions.sbatch](run_partitions.sbatch): one full 3D run per array task.
- [common.sh](common.sh): shared host-side Apptainer launch configuration.
- [container_job.sh](container_job.sh): tool setup, seed copying, and task execution.
