# Run ready partition results on CC

The partition preparation is an input to this workflow. Submission queues only
the parallel 3D evaluation jobs.

## 1. Have the input ready

For `bp_be`, the existing input folder must contain:

```text
partition_experiments/nangate45_3D/bp_be/
  preparation/results/
    1_synth.sdc
    2_2_floorplan_io.def
    2_2_floorplan_io.v
  solutions/
    s0000/macro_partition.txt
    s0000/solution.json           Optional source key and cost
    s0001/macro_partition.txt
    s0001/solution.json
    ...
```

The normal [generate_macro_partitions.py](../generate_macro_partitions.py) command
now produces this complete input layout and retains the 2D seed by default:

```bash
python3 generate_macro_partitions.py partition_experiments/nangate45_3D/bp_be/candidates.json
```

Run that preparation separately before submitting this workflow. Its matching
seed and solution files are then ready for the CC runner; no extra keep flag is
needed. Offline name-map/reference conversions still need a matching seed supplied
separately. The CC scripts evaluate the ready inputs without regenerating them.

Only the exported partitions and matching seed are required. Original candidate
JSON and macro-name alias maps are no longer inputs to job submission. Existing
solution IDs are preserved, including gaps in their numbering.

## 2. Set paths once

Edit [config.sh](config.sh) with your fixed CC paths: `CC_BASE`, `TAIWEI_BASE`,
`ORFS_BASE` and `CC_IMAGE`. Leave `CC_ACCOUNT` empty to use Slurm's default
allocation, or set it if the cluster requires an explicit project.
CPU, memory, time, platform and tier settings are in the same file.

## 3. Run from the repository on CC

```bash
# Evaluate the ready bp_be partitions, with at most four concurrent jobs.
bash cc_script/run.sh bp_be 4

# Another prepared case.
bash cc_script/run.sh bp_fe 4

# Check the ready inputs and preview without submitting.
bash cc_script/run.sh bp_be 4 --dry-run
```

One Slurm array is submitted. Each task uses a prepared partition and the existing
2D seed, starting the 3D OpenROAD flow without the 2D synthesis/partitioning
bootstrap. The existing SIF is reused. Defaults are 8 CPUs, 32 GB memory and
12 hours per candidate.

For a pilot with only the first two solution IDs, preview and then submit:

```bash
bash cc_script/run.sh bp_be 2 --limit 2 --dry-run
bash cc_script/run.sh bp_be 2 --limit 2
```

The positional `2` caps concurrency; `--limit 2` limits the total number of
partitions selected in solution-ID order. Without `--limit`, every prepared
partition is queued. The preview prints the selected IDs and `--array=0-1%2`.
Slurm schedules the two tasks independently, so they may start at different times.
Each task uses the CPU, memory and time settings in [config.sh](config.sh).

Run submission from the host shell on an allocated compute node, outside
Apptainer, with a Python module loaded. The batch jobs launch Apptainer themselves.
Set `CC_ACCOUNT` in [config.sh](config.sh) to your verified Slurm account.

After both tasks finish, inspect each task's efficiency and the array's accounting:

```bash
seff ARRAY_JOB_ID_0
seff ARRAY_JOB_ID_1
sacct -j ARRAY_JOB_ID --format=JobID,State,ExitCode,Elapsed,AllocCPUS,TotalCPU,ReqMem,MaxRSS
```

Keep step rows in the accounting output: peak memory often appears on `.batch`.
Also check each solution's `status.txt` for `state=COMPLETED` and its final
`openroad_eval.json`. Two successful candidates check the pilot paths, not every
design or partition. Use completed-job elapsed time and peak memory with some
headroom for the next request; timeouts and out-of-memory failures give lower
bounds. CPU efficiency helps identify over-allocation; compare another CPU count
before scaling out because some flow stages are serial. Two partitions provide
an initial estimate; other partitions may need more resources.

## Saved results

Submission prints the run directory and array ID. The run directory contains:

- `inputs.tar`: a frozen copy of the prepared seed and partition inputs.
- `solutions.txt`: the mapping from array indices to your original solution IDs.
- `submission.json`: solution metadata and array job ID.
- `slurm/`: scheduler logs.
- `results/s0000/r0/`: `status.txt`, `openroad_eval.json`, `run.log` and `output.tar`.

`solution.json` is optional; when present its source key and cost are recorded.
The `r0` directory is the first attempt; Slurm requeues use `r1`, `r2`, etc.

```bash
squeue --array --jobs=ARRAY_JOB_ID
```

Each job stages the runtime files, installed tools, seed and its selected partition
into `$SLURM_TMPDIR`. Synthesis is already complete; 3D placement, routing and
reports are written on local temporary storage. At exit, the job archives its
outputs and copies them and the metrics back to the shared run directory.
Keep the shared source and installed tools fixed while comparing one array's jobs.

A warning five minutes before the time limit triggers an attempt to save partial
outputs. Node failure, SIGKILL or unavailable shared storage can prevent recovery.
The implementation in `lib/` handles staging, execution and copy-back.
