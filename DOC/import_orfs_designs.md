# Using ORFS benchmarks in Taiwei3D

An ORFS benchmark supplies the RTL, macro wrappers, and timing constraints.
Taiwei3D supplies its own 2D bootstrap and tier-aware 3D technology views.
Keep these inputs separate when adapting an upstream design configuration.

## Rules for importing a Nangate45 design

Preserve the design's logical definition and timing intent, adapt the
technology views to Taiwei's stages, and choose physical/optimization settings
explicitly. An existing Taiwei design is a structural template, rather than
a complete list of supported parameters. A parameter missing from Ariane133
is not automatically unsupported, unnecessary, or safe to remove from every
other design.

### Two configurations, with different responsibilities

| Configuration | Platform | Responsibility |
| --- | --- | --- |
| `config2d.mk` | `nangate45` | Read RTL and SDC, synthesize, build the initial 2D floorplan, place IOs, and partition. Use unsuffixed 2D cell/macro models. |
| `config.mk` | `nangate45_3D` | Read the synthesized/partitioned handoff and perform tier-aware implementation. Use bottom/upper models and inactive-tier COVER views. |

Both files live under `designs/nangate45_3D/<nickname>/`. Their
`DESIGN_NAME` must match the actual RTL top module, and their
`DESIGN_NICKNAME` must match the case directory used by the launch wrappers.
The top name and nickname can differ, as for `bp_be_top` and `bp_be`.

The [launcher](../test/openroad/ORD_3D_NEW_FLOW.sh) selects `config2d.mk`
for the bootstrap and `config.mk` for later stages, using separate make
processes. Exporting a value inside the 2D make does not change the parent
launcher's environment. Put a later-stage option in the 3D configuration,
or supply it through a shared environment/command-line mechanism accepted
by that setting. The 3D configuration does not automatically include the
2D configuration or reread its RTL sources.

### How to handle each upstream input or setting

| Upstream item | Import rule | Reason for differences |
| --- | --- | --- |
| RTL sources, RAM wrappers, include paths, defines, and top parameters | Preserve all required definitions and options. Rewrite paths to local copies. | Missing wrappers or changed defines/parameters can change the circuit or prevent elaboration. |
| `SDC_FILE` | Preserve clock, IO-delay, and timing-exception intent. Check that referenced ports and hierarchical objects still exist. | The SDC defines the timing problem; substituting another design's SDC changes that problem. Its filename is arbitrary if `SDC_FILE` points to it. |
| `ADDITIONAL_LEFS`, `ADDITIONAL_LIBS` | Resolve models against Taiwei's local platform layout. | ORFS and Taiwei use different subdirectories. The 3D stages also need tier-specific masters. |
| Original `config.mk` | Translate it into the two active Taiwei configurations. A reference copy is optional. | The original ORFS file describes a different stage graph and library layout. |
| `IO_CONSTRAINTS` and a design-specific `io.tcl` | Omit for Taiwei's existing common pin-placement policy, or explicitly implement and validate support for those constraints. | Current Taiwei pin placement does not consume `IO_CONSTRAINTS`; its OpenROAD script clears old constraints and places pins itself. Adding only the variable has no effect. |
| `DIE_AREA`, `CORE_AREA`, `CORE_UTILIZATION` | Select the floorplan policy explicitly; record any change from ORFS. | The OpenROAD 2D floorplanner prioritizes `FLOORPLAN_DEF`, then utilization, then explicit die/core areas. The OpenROAD 3D floorplanner normally recomputes the footprint from the larger tier's area and target utilization. |
| `MACRO_PLACE_HALO`, `PLACE_DENSITY`, `PLACE_DENSITY_LB_ADDON` | Choose appropriate values for the stage; inspect platform defaults when omitted. | These affect physical implementation. In this flow, a configured density addon can make the density helper derive a value instead of simply using `PLACE_DENSITY`. |
| `RTLMP_*` | Preserve or retune deliberately where hierarchical macro placement runs. | Taiwei supports these options in its macro-placement helper. Omitting them selects tool defaults, rather than disabling macro placement. |
| `SKIP_GATE_CLONING`, `TNS_END_PERCENT`, other repair options | Check the consumer and set them in the configuration for the intended stage. | Taiwei's timing-repair helper supports these examples. Omitting them can change repair behavior; their absence from a sample is not a migration requirement. |
| `ABC_AREA`, synthesis hierarchy/frontend controls | Check support in Taiwei's synthesis scripts before preserving or changing them. | Mapping effort, hierarchy, and frontend choices can change implementation behavior or compatibility. Functional defines and parameters must remain correct. |
| `FASTROUTE_TCL`, layer restrictions, PDN and RC settings | Use compatible Taiwei platform/3D settings; port any essential custom behavior explicitly. | Original 2D scripts can assume a different routing stack, layer naming, or power network. |
| `rules-base.json` | Keep as an optional original 2D reference. | Its numbers do not establish acceptance thresholds for the new 3D implementation. |
| Upstream license and provenance | Retain the supplied notices and identify source revisions and local modifications. | Imported sources should remain attributable and reproducible. |

Relevant consumers are the [2D floorplanner](../scripts_openroad/floorplan.tcl),
[3D floorplanner](../scripts_openroad/floorplan_3d.tcl),
[IO placer](../scripts_openroad/io_place.tcl),
[density/halo helpers](../scripts_openroad/placement_utils.tcl),
[macro-placement helper](../scripts_openroad/place_macro_util.tcl), and
[timing-repair helper](../scripts_openroad/util.tcl).

### Source and macro-library completeness

A separate wrapper file is optional; its module definitions are not. Either
list the main RTL and wrapper files in `VERILOG_FILES`, or combine them into
one source file and list that file once. For example, Ariane133 already
contains its wrappers, while the imported Ariane136 needs its local
`macros.v` unless those definitions are merged into its main RTL.

Verilog supplies macro instances and wrapper logic. Liberty supplies the
cell interface and timing/electrical model, and LEF supplies its physical
abstract. Loading a Liberty/LEF model does not supply an unrelated missing
Verilog wrapper module.

For each macro type actually used, verify its matching 2D LEF/Liberty,
bottom and upper LEF/Liberty, and both inactive-tier COVER LEFs. Check cell
names and pin interfaces as well as file existence. If a required type lacks
3D views, the import needs platform enablement before that design can run
through the 3D stages. Wildcards only collect existing files; they do not
create missing models or instantiate every model they load.

In the attached-style 3D configuration, `SC_LEF_UPPER_COVER` pairs the normal
bottom view with an upper COVER view, and `SC_LEF_BOTTOM_COVER` pairs a bottom
COVER view with the normal upper view. The `ADDITIONAL_LEFS_*_COVER` entries
do the same for macros. These physical abstractions preserve connectivity
while providing the appropriate view for tier-local operations.

### Configuration conventions and defaults

Use a nearby macro design such as [bp_quad](../designs/nangate45_3D/bp_quad/)
as the 3D structural template, then set the top/nickname and validate all
macro views. Do not treat the example's utilization or optimization knobs
as values proved optimal for the new design. In particular, 60% for the 2D
bootstrap and 50% for 3D are example policies, not import requirements.

Use valid assignments such as `export GEN_EFF = medium` and
`export MAP_EFF = high`. The older spelling `export GEN_EFF medium` exports
variable names without assigning `medium`. Also distinguish `?=`, which
assigns only when a variable is undefined, from a normal `=` assignment.

Check the exact parameter name read by each backend. For example, the
current OpenROAD 2D floorplanner reads `CORE_ASPECT_RATIO`, while its 3D
floorplanner reads `ASPECT_RATIO`. The common value 1 masks this difference
in the existing square examples. Likewise, a variable understood by the
OpenROAD scripts need not be consumed by the Cadence scripts.

For every difference from ORFS, record whether it is a path/layout change,
a required 3D technology adaptation, an explicit implementation policy,
a tool-default fallback, or a change in the evaluation problem. This makes
the new configuration reviewable without claiming an untested setting is
necessary or beneficial.

## Ariane136 on Nangate45

The [2D configuration](../designs/nangate45_3D/ariane136/config2d.mk) reads
benchmark inputs checked into the
[case directory](../designs/nangate45_3D/ariane136/), following the existing
Ariane133 layout:

- [ariane.v](../designs/nangate45_3D/ariane136/ariane.v): converted Verilog,
  copied unchanged from ORFS's `designs/src/ariane136/ariane.sv2v.v` and
  renamed locally. Its top module is `ariane`.
- [macros.v](../designs/nangate45_3D/ariane136/macros.v): upstream RAM wrappers.
- [constraint.sdc](../designs/nangate45_3D/ariane136/constraint.sdc): upstream
  clock and IO timing constraints, specifying a 6 ns clock on `clk_i`.

The design nickname is `ariane136`; the top module remains `ariane`.
Benchmark sources no longer depend on an external ORFS checkout. Taiwei
runs use `config2d.mk` and `config.mk`; an original ORFS configuration or
IO-placement script is not needed by the active case. The
[2D baseline rules](../designs/nangate45_3D/ariane136/rules-base.json) remain
an upstream reference.

The wrappers instantiate `fakeram45_256x16`. Taiwei's Nangate45 platform has
the unsuffixed LEF and Liberty model for 2D synthesis/floorplanning. The
[3D configuration](../designs/nangate45_3D/ariane136/config.mk) selects the
existing bottom and upper RAM LEFs, Liberty models, and inactive-tier COVER
views. No new RAM views are needed for this benchmark.

The current case uses 60% utilization for the 2D bootstrap, 50% for 3D, and
Taiwei's normal IO placement. It does not reproduce ORFS's fixed
1500-by-1500 die or the edge restrictions in its original IO script.
Timing constraints and RAM wrappers are the unchanged local copies from
ORFS. Physical results therefore describe the
Taiwei flow configuration, rather than an identical ORFS 2D floorplan.

From the Taiwei repository root, set up the tool paths:

```bash
export ORFS_DIR=/home/swang848/ORFS-Research
source env.sh
```

`ORFS_DIR` selects the installed toolchain; the design inputs themselves are
stored in this repository.

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
evaluation wrapper. The current ORD evaluation wrapper invokes `ord-final`;
it does not require Cadence. The CDS wrappers use Cadence tools.

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

Validation confirmed that all referenced input and library-view files
exist locally, and Yosys successfully read the copied RTL and wrappers and resolved
the `ariane` hierarchy. Yosys emitted warnings about continuous assignments to
`reg` signals in the upstream converted Verilog. Shell syntax checks and make
dry runs passed. Full synthesis, partitioning, and 3D place-and-route have not
been run for this case.

## BlackParrot back-end, front-end, and multi cases

These cases use the same local-input layout and sorted RAM library wildcards
as the existing Nangate45 macro designs:

| Taiwei case | ORFS source case / top module | Original clock period |
| --- | --- | --- |
| [bp_be](../designs/nangate45_3D/bp_be/) | `bp_be_top` | 2.6 ns |
| [bp_fe](../designs/nangate45_3D/bp_fe/) | `bp_fe_top` | 1.8 ns |
| [bp_multi](../designs/nangate45_3D/bp_multi/) | `bp_multi_top` | 4.8 ns |

Each case contains `config2d.mk`, `config.mk`, a single `<top_module>.v`, the
unchanged upstream `constraint.sdc`, `rules-base.json`, and its upstream
`LICENSE`. The Verilog combines ORFS's `designs/src/<top_module>/pickled.v`
with `designs/nangate45/<top_module>/macros.v`, so all RAM wrapper definitions
are included without a separate wrapper file. The RTL headers retain the
upstream project and revision information. The JSON rules are retained as
the upstream 2D reference, rather than as acceptance thresholds for 3D.

The 2D defaults follow the supplied Ariane-style configuration: 60% core
utilization, aspect ratio 1, core margin 5 um, and 32 cores. The 3D defaults
follow the existing `bp_quad`/`ariane133` macro flow: 50% utilization,
aspect ratio 1, core margin 5 um, and macro halos of 5 um on both axes.
The `bp_multi` synthesis configuration retains upstream `ABC_AREA=1`.
These are initial configurations; no performance improvement is claimed.
Upstream fixed die sizes, IO edge restrictions, and routing-layer scripts
are replaced by Taiwei's existing floorplan, IO, and merged-stack routing
policies. They are not copied into unused design-specific files.

All required RAM types already have local 2D, bottom-tier, upper-tier,
and inactive-tier COVER views:

- `bp_be`: `fakeram45_512x64`, `fakeram45_64x15`, `fakeram45_64x96`.
- `bp_fe`: `fakeram45_512x64`, `fakeram45_64x7`, `fakeram45_64x96`.
- `bp_multi`: `fakeram45_512x64`, `fakeram45_256x96`, `fakeram45_32x64`,
  `fakeram45_64x7`, `fakeram45_64x15`, `fakeram45_64x96`.

The imported `bp_multi` wrapper named `hard_mem_1r1w_d32_w64_wrapper`
retains ORFS's documented single-port placeholder for a read/write-port RAM.
Its undeclared address signal has been corrected to an explicit 5-bit
`addr_int`, matching the wrapper's read and write address ports. This fixes
address truncation while retaining the upstream memory-model approximation.

Run the cases using the normal launch wrappers:

```bash
source env.sh
bash test/nangate45_3D/bp_be/ord/run.sh
bash test/nangate45_3D/bp_fe/ord/run.sh
bash test/nangate45_3D/bp_multi/ord/run.sh
```

Alternatively, the experiment runner discovers the wrappers automatically:

```bash
python3 run_experiments.py --flow ord --tech nangate45_3D \
  --case bp_be --case bp_fe --case bp_multi --list
```

Replace `--list` with `--run-only` to run OpenROAD without the additional
evaluation step. The matching `cds/run.sh` and `cds/eval.sh` wrappers are
also present for installations with Cadence tools and licenses.

Initial validation passed Yosys hierarchy checks for all three top modules,
local input and required tier-library checks, and shell syntax checks for
the launch wrappers. The copied SDC files match the upstream files. Full
synthesis, partitioning, and 3D place-and-route have not been run.

## Adapting another ORFS Nangate45 design

1. Inspect its upstream `config.mk` for the actual top module, nickname,
   source files, include directories/defines, RAM wrappers, and SDC.
2. Copy the RTL, RAM wrappers, constraints, license, and source notes from the
   upstream source/design directories into `designs/nangate45_3D/<nickname>/`.
   Add `config2d.mk`, with `PLATFORM=nangate45`, and point its sources and
   constraints to those local copies. Resolve physical/timing libraries
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
