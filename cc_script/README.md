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
