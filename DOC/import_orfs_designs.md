# Using ORFS benchmarks in Taiwei3D

An ORFS benchmark supplies the RTL, macro wrappers, and timing constraints.
Taiwei3D supplies its own 2D bootstrap and tier-aware 3D technology views.
Keep these inputs separate when adapting an upstream design configuration.

## Ariane136 on Nangate45

The [2D configuration](../designs/nangate45_3D/ariane136/config2d.mk) reads
these files from `ORFS_DESIGN_HOME`, which defaults to
`$(ORFS_DIR)/flow/designs`:

- `src/ariane136/ariane.sv2v.v`: converted Verilog, with top module `ariane`.
- `nangate45/ariane136/macros.v`: RAM wrappers required by the RTL.
- `nangate45/ariane136/constraint.sdc`: upstream clock and IO timing constraints
  (the inspected ORFS checkout specifies a 6 ns clock on `clk_i`).

The design nickname is `ariane136`; the top module remains `ariane`.
The external ORFS source checkout must remain available during synthesis.
`ORFS_DESIGN_HOME` can point to a separate benchmark checkout if needed.

The wrappers instantiate `fakeram45_256x16`. Taiwei's Nangate45 platform has
the unsuffixed LEF and Liberty model for 2D synthesis/floorplanning. The
[3D configuration](../designs/nangate45_3D/ariane136/config.mk) selects the
existing bottom and upper RAM LEFs, Liberty models, and inactive-tier COVER
views. No new RAM views are needed for this benchmark.

The case uses Taiwei's utilization-based floorplan (50% by default) and its
normal IO placement. It does not reproduce ORFS's fixed 1500-by-1500 die or
the edge restrictions in its `io.tcl`. Timing constraints and RAM wrappers
are read unchanged from ORFS. Physical results therefore describe the
Taiwei flow configuration, rather than an identical ORFS 2D floorplan.

From the Taiwei repository root, set up the tool paths:

```bash
export ORFS_DIR=/home/swang848/ORFS-Research
source env.sh
```

To run the complete OpenROAD flow directly:

```bash
bash test/nangate45_3D/ariane136/ord/run.sh
```

The [experiment runner](../run_experiments.py) also discovers the new wrappers:

```bash
python3 run_experiments.py --flow ord --tech nangate45_3D --case ariane136 --list
python3 run_experiments.py --flow ord --tech nangate45_3D --case ariane136 --run-only
```

`--run-only` runs through OpenROAD final reporting and skips the additional
evaluation wrapper, which uses Cadence. Without `--run-only`, the ORD case's
evaluation also requires Innovus.

For a staged first run, bootstrap synthesis, floorplanning, and partitioning:

```bash
bash test/common/run_stage.sh nangate45_3D openroad openroad ariane136 ord-3d-flow-2dpart
```

After that command succeeds, continue from the generated partition:

```bash
SKIP_2D_PART=1 bash test/nangate45_3D/ariane136/ord/run.sh
```

The default OpenROAD outputs are under
`results/nangate45_3D/ariane136/openroad/`, with matching design/variant
directories under `logs/` and `reports/`.

Cadence wrappers are provided using the same conventions as other cases:

```bash
bash test/nangate45_3D/ariane136/cds/run.sh
```

This requires the configured Cadence tools and licenses. Adding a wrapper
does not establish that the benchmark passes a complete flow. In particular,
the repository's [known macro-case issues](issue.md) still apply.

Initial validation confirmed that all referenced input and library-view files
exist, and Yosys successfully read the upstream RTL and wrappers and resolved
the `ariane` hierarchy. Yosys emitted warnings about continuous assignments to
`reg` signals in the upstream converted Verilog. Shell syntax checks and make
dry runs passed. Full synthesis, partitioning, and 3D place-and-route have not
been run for this case.

## Adapting another ORFS Nangate45 design

1. Inspect its upstream `config.mk` for the actual top module, nickname,
   source files, include directories/defines, RAM wrappers, and SDC.
2. Add `designs/nangate45_3D/<nickname>/config2d.mk`, with `PLATFORM=nangate45`.
   Point sources and constraints to ORFS, but resolve physical/timing libraries
   against Taiwei's local platform. ORFS and Taiwei library subdirectories can
   differ; for example, Taiwei stores the 2D RAM views under `lef/fakeram/`
   and `lib/fakeram/`.
3. Add `config.mk`, with `PLATFORM=nangate45_3D`. For every hard macro, verify
   that both tiers have matching LEF/Liberty views and that inactive-tier
   COVER views exist. A new macro type can require platform enablement.
4. Copy the small run/eval wrappers into `test/nangate45_3D/<nickname>/ord/`
   and, if needed, `cds/`. The wrappers derive the nickname from their path;
   the experiment runner discovers their `run.sh` files automatically.
5. Validate synthesis and partitioning first, then 3D view generation,
   macro placement, CTS, and routing. Tune utilization and macro halos only
   after checking these stage outputs.

Do not reuse upstream `rules-base.json` as a 3D acceptance baseline: it
describes the upstream 2D flow and its physical settings.
