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
`ORFS_BASE` and `CC_IMAGE`. `CC_INPUT_ROOT` defaults to
`$TAIWEI_BASE/partition_experiments`; cases are read from
`$CC_INPUT_ROOT/<platform>/<case>/`. `CC_OUTPUT_ROOT` defaults to `$SCRATCH/taiwei`
and must stay below `$SCRATCH`; saved outputs are not written into the repository.
Leave `CC_ACCOUNT` empty to use Slurm's default
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

The entry script resolves its own location, so it also works when launched by
absolute path from another directory. Invalid settings and missing prepared inputs
are rejected before a run directory is created or jobs are submitted.

One Slurm array is submitted. Each task uses a prepared partition and the existing
2D seed, starting the 3D OpenROAD flow without the 2D synthesis/partitioning
bootstrap. The existing SIF is reused. Defaults are 8 CPUs, 16 GiB memory and
2 hours per candidate, based on the `bp_be` pilot:

| Solution | Elapsed | Peak memory | CPU efficiency with 8 cores |
| --- | --- | --- | --- |
| s0000 | 1:11:20 | 10.17 GiB | 74.73% |
| s0001 | 1:08:33 | 9.54 GiB | 73.25% |

Remeasure for other designs. Six cores is a useful next scaling experiment;
the pilot measured eight cores, so its runtime does not establish six-core performance.

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
Check Slurm's final state and each solution's `openroad_eval.json`.
Two successful candidates check the pilot paths, not every
design or partition. Use completed-job elapsed time and peak memory with some
headroom for the next request; timeouts and out-of-memory failures give lower
bounds. CPU efficiency helps identify over-allocation; compare another CPU count
before scaling out because some flow stages are serial. Two partitions provide
an initial estimate; other partitions may need more resources.

## Saved results

Submission prints the scratch run directory and array ID. Each run contains
only `logs/` and `results/`:

```text
$SCRATCH/taiwei/
  RUN_ID/
    logs/
      submission.json
      solutions.txt
      partition-ARRAY_JOB_ID_0.out
      partition-ARRAY_JOB_ID_1.out
    results/
      s0000/
        openroad_eval.json
        output.tar
      s0001/
        openroad_eval.json
        output.tar
```

Jobs copy the shared prepared seed and their selected partition directly from
the existing input folders when they start. There is no intermediate input archive.
Keep prepared inputs unchanged while jobs are queued or running.
The solution list under `logs/` maps array indices to the original solution IDs.
Submission metadata records the input directory, those IDs, optional source keys
and costs, the array ID, and requested resources.

`solution.json` is optional; when present its source key and cost are recorded.
Each partition has one flat result directory. Automatic requeue is disabled;
retry failed candidates through a new submission so saved results stay intact.
The scheduler log records the workspace and final state, exit code, and any
interruption signal; there is no separate status file.

The full flow log is stored once as `run.log` inside `output.tar`, alongside the
flow results, reports, and stage logs. To inspect it on an allocated compute node:

```bash
tar -xOf results/s0000/output.tar run.log
```

```bash
squeue --array --jobs=ARRAY_JOB_ID
```

Each job stages the runtime files, installed tools, seed and its selected partition
into `$SLURM_TMPDIR`. Synthesis is already complete; 3D placement, routing and
reports are written on local temporary storage. At exit, the job archives its
outputs and saves them and the metrics to the run directory under `$SCRATCH/taiwei`.
The staged ORFS tree includes `tools/install`, `dependencies/lib`, and
`dependencies/lib64` when present. These matching shared libraries are required
by OpenROAD; the container checks `openroad -version` before starting the flow.
Keep the shared source and installed tools fixed while comparing one array's jobs.
Scratch is temporary storage; copy important completed results to project storage
for long-term retention.

A warning five minutes before the time limit triggers an attempt to save partial
outputs. Node failure, SIGKILL or unavailable shared storage can prevent recovery.
The implementation in `lib/` handles staging, execution and copy-back.

## Summarize final PPA

After extracting the result archives, run the standard-library-only
[summarize_partitions.py](../summarize_partitions.py) from the repository root:

```bash
python3 summarize_partitions.py cc_results/20261107T012451Z_dw7hy6ps
```

The default output is `partition_ppa.csv` inside the input directory. To choose
another output path:

```bash
python3 summarize_partitions.py cc_results/20261107T012451Z_dw7hy6ps -o partition_ppa.csv
```

The input can also be the run's `results/` directory or one `sNNNN/` partition
directory. Each CSV row represents one partition, sorted by its original numeric
index (`s0048` becomes `idx=48`; gaps are preserved). The columns are:

| Column | Meaning / unit |
| --- | --- |
| `idx` | Numeric partition index from the `sNNNN` folder name |
| `after_routing_wirelength` | Final detailed-route wirelength, micrometers |
| `tns` | Final setup total negative slack, nanoseconds |
| `wns` | Final setup worst negative slack, nanoseconds |
| `power` | Final estimated total power, watts |
| `cost` | L2 norm of the four min-max normalized PPA metrics; lower is better |

Normalization uses the minimum and maximum of each metric across complete rows
in this invocation. Wirelength and power use `(value - min) / (max - min)`;
TNS and WNS use `(max - value) / (max - min)` because higher slack is better.
The cost is `sqrt(wire_norm^2 + tns_norm^2 + wns_norm^2 + power_norm^2)`, with
equal weight for all four metrics. Its range is 0 to 2, with 0 representing the
best observed value in every metric. A constant metric contributes zero; a run
with only one complete partition therefore has cost zero. Incomplete rows have
blank cost and are excluded from normalization bounds. Scores are relative to
the partitions included in this CSV, so adding partitions can change the costs.

Timing and power come from `openroad_eval.json`, with missing values filled from
`final_summary.txt` or `6_finish.rpt`. Wirelength comes from `final_summary.txt`,
or the sum of per-net `drt:` lengths in `6_wire_length.rpt` when needed. Placement
HPWL and intermediate routing iterations are not used. The program reads the
files inside each partition folder, ignoring old container paths in JSON metadata.

Failed or incomplete partitions retain their row with blank missing measurements
and a warning on stderr; a genuine measured zero remains zero. Multiple report
variants inside one partition are rejected to avoid mixing different runs.

## Plot partition quality

Use [plot_partitions.py](../plot_partitions.py) to plot all solutions with cut
size on the x axis, area imbalance on the y axis, and a final PPA metric as the
point color. Install its plotting dependency in your Python environment:

```bash
python3 -m pip install matplotlib
python3 plot_partitions.py \
  partition_experiments/nangate45_3D/bp_be/solutions \
  cc_results/20261107T012451Z_dw7hy6ps/partition_ppa.csv
```

The default output is `partition_wns.png` beside the CSV, at 300 dpi. Use
`-o figure.pdf` or `-o figure.svg` for vector output, or `-o figure.png` to choose
another PNG path.

For TNS colors and rankings, add `--metric tns`:

```bash
python3 plot_partitions.py \
  partition_experiments/nangate45_3D/bp_be/solutions \
  cc_results/20261107T012451Z_dw7hy6ps/partition_ppa.csv --metric tns
```

This saves `partition_tns.png` beside the CSV. WNS remains the default metric.

Routed wirelength and power use `--metric rwl` and `--metric power`:

```bash
python3 plot_partitions.py \
  partition_experiments/nangate45_3D/bp_be/solutions \
  cc_results/20261107T012451Z_dw7hy6ps/partition_ppa.csv --metric rwl
python3 plot_partitions.py \
  partition_experiments/nangate45_3D/bp_be/solutions \
  cc_results/20261107T012451Z_dw7hy6ps/partition_ppa.csv --metric power
```

These save `partition_rwl.png` and `partition_power.png` beside the CSV.

To combine all four metrics in one figure, use `--metric merge`:

```bash
python3 plot_partitions.py \
  partition_experiments/nangate45_3D/bp_be/solutions \
  cc_results/20261107T012451Z_dw7hy6ps/partition_ppa.csv --metric merge
```

The output is `partition_merge.png`. The 2×2 layout contains WNS at upper left,
TNS at upper right, rWL at bottom left, and Power at bottom right. Panels touch
without horizontal or vertical gaps, and legends use larger text and markers. Axis titles
appear once on the outside left and bottom margins, with tick labels only on
the outside axes. The merged figure omits colorbars and keeps each point's
metric color, the top-five labels, and the Balanced Min-Cut star. Color scales
match those in the individual metric plots. PDF and SVG output also support
merge mode through `-o`.

| Metric option | CSV column | Colorbar unit |
| --- | --- | --- |
| `wns` | `wns` | ns |
| `tns` | `tns` | ns |
| `rwl` | `after_routing_wirelength` | µm |
| `power` | `power` | W |

The program reads `[cut size, area imbalance]` from each `sNNNN/solution.json`
file's `cost` array and joins its folder index to the CSV's `idx`. This geometry
array is separate from the CSV's scalar PPA `cost`. The five selected metric
values closest to zero are labeled **1–5**, with rank 1 closest, using ascending
absolute values. For routed wirelength and power, lower values rank first.
For example, `-0.48` ranks ahead of `-1.01`. Ties use the original partition
index; labels are ranks rather than partition IDs. Blue consistently represents
better values and red worse values: WNS/TNS nearer zero appear bluer, and lower
routed wirelength/power appear bluer. Solutions without the selected metric are
omitted from the plot and ranking. The CSV's `cost` column is optional for this
plot. The console lists the labeled ranks, partition indices, and selected metric. Use
`--top-n N` to change the label count.

The solution with the smallest area imbalance is drawn as a star labeled
**Balanced Min-Cut**. If several solutions share that imbalance, the smallest
cut size wins, followed by the original partition index. Selection uses all
solution geometries so the reference solution is consistent across metrics.
The star replaces its dot and retains the metric color. If that solution lacks
the selected metric, it remains hidden and the console explains why. Its
top-five rank label is retained when applicable.

## Runner code

[run.sh](run.sh) loads [config.sh](config.sh) and starts
[lib/submit.py](lib/submit.py). Submission validates settings, reads the prepared
inputs, selects partitions, creates the scratch run, and submits one Slurm array.
Its metadata is written atomically under `logs/`.

[lib/job.sbatch](lib/job.sbatch) stages the runtime and selected inputs, runs the
container, and saves the result archive and final metrics. It also saves partial
artifacts on failure or interruption. [lib/container.sh](lib/container.sh) sources
the project environment, prepares the design from the copied seed, and runs OpenROAD.
