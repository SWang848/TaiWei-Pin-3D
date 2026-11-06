#!/usr/bin/env bash
# Runs entirely in the job-local repository mounted at /TaiWei-Pin-3D.
set -euo pipefail

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $# == 7 ]] || fail 'Use cc_script/run.sh to schedule these jobs.'
platform="$1"
design="$2"
run_relative="$3"
export STD_CELL_TIER="$4"
export OUTER_ITERATIONS="$5"
[[ "$platform" =~ ^[A-Za-z0-9_-]+$ && "$design" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'Invalid platform or case.'
[[ "$STD_CELL_TIER" == bottom || "$STD_CELL_TIER" == upper ]] || fail 'Invalid standard-cell tier.'
[[ "$OUTER_ITERATIONS" =~ ^[1-9][0-9]*$ ]] || fail 'Invalid outer iteration count.'

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo"
run_dir="$(realpath -e -- "$run_relative")"
[[ "$run_dir" == "$repo/partition_experiments/$platform/$design/cc_runs/"* ]] || fail 'Run directory is outside this case.'

export ORFS_DIR=/ORFS-Research
export OPENROAD_EXE="$ORFS_DIR/tools/install/OpenROAD/bin/openroad"
export YOSYS_EXE="$ORFS_DIR/tools/install/yosys/bin/yosys"
export STA_EXE="$ORFS_DIR/tools/install/OpenROAD/bin/sta"
export PATH="$ORFS_DIR/tools/install/OpenROAD/bin:$ORFS_DIR/tools/install/yosys/bin:$PATH"
export PYTHON_EXE="$(command -v python3)"
source ./env.sh

solution_id="$6"
variant="$7"
[[ "$solution_id" =~ ^s[0-9]+$ && "$variant" =~ ^[A-Za-z0-9_]+$ ]] || fail 'Invalid solution or variant.'
export MACRO_PARTITION_FILE="$run_dir/solutions/$solution_id/macro_partition.txt"
[[ -s "$MACRO_PARTITION_FILE" ]] || fail "Missing partition: $MACRO_PARTITION_FILE"
seed="$run_dir/preparation/results"
seed_files=(1_synth.sdc 2_2_floorplan_io.def 2_2_floorplan_io.v)
for file in "${seed_files[@]}"; do
  [[ -s "$seed/$file" ]] || fail "Missing prepared seed: $seed/$file"
done

result_dir="$repo/results/$platform/$design/$variant"
report_dir="$repo/reports/$platform/$design/$variant"
# Claim this variant before updating status or copying anything. A duplicate
# worker must not change the original worker's results or completion record.
for tree in results logs objects reports; do
  [[ ! -e "$repo/$tree/$platform/$design/$variant" ]] || fail "Variant already exists: $tree/$platform/$design/$variant"
done
mkdir -p "$(dirname "$result_dir")"
mkdir "$result_dir"
for file in "${seed_files[@]}"; do cp -- "$seed/$file" "$result_dir/$file"; done
cp -- "$MACRO_PARTITION_FILE" "$result_dir/partition.txt"

export PIN3D_VIEW_IMPL=tcl
export SKIP_2D_PART=1
unset START_FROM REUSE_2DPART_FROM_VARIANT
printf '[cc] solution=%s variant=%s partition=%s\n' "$solution_id" "$variant" "$MACRO_PARTITION_FILE"
bash test/openroad/ORD_3D_NEW_FLOW.sh "$platform" "$variant" openroad "$design"
[[ -s "$report_dir/openroad_eval.json" ]] || fail 'Flow returned without its final OpenROAD metrics JSON.'
