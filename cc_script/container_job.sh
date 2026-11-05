#!/usr/bin/env bash
# Runs inside the existing SIF; used by the two Slurm entry points.
set -euo pipefail

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

mode="${1:-}"
shift || true
case "$mode:$#" in
  prepare:5|run:7) ;;
  *) fail 'Use the submit_partitions.py entry point to schedule these jobs.' ;;
esac
platform="$1"
design="$2"
run_relative="$3"
export STD_CELL_TIER="$4"
export OUTER_ITERATIONS="$5"
[[ "$platform" =~ ^[A-Za-z0-9_-]+$ && "$design" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'Invalid platform or case.'
[[ "$STD_CELL_TIER" == bottom || "$STD_CELL_TIER" == upper ]] || fail 'Invalid standard-cell tier.'
[[ "$OUTER_ITERATIONS" =~ ^[1-9][0-9]*$ ]] || fail 'Invalid outer iteration count.'

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

if [[ "$mode" == prepare ]]; then
  exec "$PYTHON_EXE" generate_macro_partitions.py "$run_dir/candidates.json" \
    --design-config "designs/$platform/$design/config2d.mk" --keep-preparation
fi

task_index="$6"
variant="$7"
[[ "$task_index" =~ ^[0-9]+$ && "$variant" =~ ^[A-Za-z0-9_]+$ ]] || fail 'Invalid array task or variant.'
printf -v solution_id 's%04d' "$task_index"
export MACRO_PARTITION_FILE="$run_dir/solutions/$solution_id/macro_partition.txt"
[[ -s "$MACRO_PARTITION_FILE" ]] || fail "Missing partition: $MACRO_PARTITION_FILE"
seed="$run_dir/preparation/results"
seed_files=(1_synth.sdc 2_2_floorplan_io.def 2_2_floorplan_io.v 2_2_floorplan_io.odb)
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
status_dir="$run_dir/task_status"
mkdir -p "$status_dir"
status_file="$status_dir/${solution_id}_${variant}.tsv"
write_status() {
  printf 'solution_id\tflow_variant\tstate\texit_code\teval_json\n%s\t%s\t%s\t%s\t%s\n' \
    "$solution_id" "$variant" "$1" "$2" \
    "reports/$platform/$design/$variant/openroad_eval.json" > "${status_file}.tmp"
  mv -- "${status_file}.tmp" "$status_file"
}
finish() {
  local code=$?
  trap - EXIT
  if (( code == 0 )); then write_status COMPLETED 0; else write_status FAILED "$code"; fi
  exit "$code"
}
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
write_status RUNNING ''

for file in "${seed_files[@]}"; do cp -- "$seed/$file" "$result_dir/$file"; done
cp -- "$MACRO_PARTITION_FILE" "$result_dir/partition.txt"

export PIN3D_VIEW_IMPL=tcl
export SKIP_2D_PART=1
unset START_FROM REUSE_2DPART_FROM_VARIANT
printf '[cc] solution=%s variant=%s partition=%s\n' "$solution_id" "$variant" "$MACRO_PARTITION_FILE"
bash test/openroad/ORD_3D_NEW_FLOW.sh "$platform" "$variant" openroad "$design"
[[ -s "$report_dir/openroad_eval.json" ]] || fail 'Flow returned without its final OpenROAD metrics JSON.'
