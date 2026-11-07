#!/usr/bin/env bash
# One prepared partition per array task. Final outputs stay under CC_OUTPUT_ROOT.
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --job-name=taiwei

set -euo pipefail
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $# == 5 ]] || fail 'Submit using bash cc_script/run.sh CASE PARALLEL_JOBS.'
platform="$1"; design="$2"; run_directory="$3"; tier="$4"; iterations="$5"
[[ "$platform" =~ ^[A-Za-z0-9_-]+$ && "$design" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'Invalid platform or case.'
: "${TAIWEI_BASE:?Submit using cc_script/run.sh.}"
: "${ORFS_BASE:?Set ORFS_BASE in cc_script/config.sh.}"
: "${CC_IMAGE:?Set CC_IMAGE in cc_script/config.sh.}"
: "${CC_OUTPUT_ROOT:?Set CC_OUTPUT_ROOT in cc_script/config.sh.}"
: "${SLURM_JOB_ID:?Run this script through Slurm.}"
: "${SLURM_CPUS_PER_TASK:?Missing Slurm CPU allocation.}"
: "${SLURM_TMPDIR:?Node-local storage is required; no shared-filesystem fallback is used.}"
: "${SLURM_ARRAY_TASK_ID:?Missing array index.}"
: "${SLURM_ARRAY_JOB_ID:?Missing array job ID.}"

code_root="$(realpath -e "$TAIWEI_BASE")"
input_root="$(realpath -e "${CC_INPUT_ROOT:-$code_root/partition_experiments}")"
input_dir="$input_root/$platform/$design"
output_root="$(realpath -e "$CC_OUTPUT_ROOT")"
run_dir="$(realpath -e -- "$run_directory")"
[[ "$(dirname "$run_dir")" == "$output_root" ]] || fail 'Run directory is outside CC_OUTPUT_ROOT.'
[[ -r "$CC_IMAGE" ]] || fail "Cannot read prebuilt SIF: $CC_IMAGE"
solution_id="$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$run_dir/logs/solutions.txt")"
[[ "$solution_id" =~ ^s[0-9]+$ ]] || fail 'Array index has no prepared solution.'
variant="cc_${SLURM_ARRAY_JOB_ID}_${solution_id}"
result_dir="$run_dir/results/$solution_id"
mkdir -p "$(dirname "$result_dir")"
mkdir "$result_dir"  # Claim once: duplicate workers cannot overwrite saved results.

workspace="$(mktemp -d "$SLURM_TMPDIR/taiwei.XXXXXX")"
local_repo="$workspace/TaiWei-Pin-3D"
input_relative=.cc_inputs
local_inputs="$local_repo/$input_relative"
mkdir -p "$local_inputs/preparation/results" "$local_inputs/solutions/$solution_id" \
         "$workspace/ORFS-Research" "$workspace/tmp"
touch "$workspace/run.log"
child_pid=""
interrupted=""

stage_runtime() {
  local -a code_files=(Makefile env.sh designs platforms scripts_openroad util test cc_script/lib)
  local -a tool_files=(tools/install dependencies/lib)
  [[ ! -d "$ORFS_BASE/dependencies/lib64" ]] || tool_files+=(dependencies/lib64)
  tar -C "$code_root" -cf - "${code_files[@]}" | tar -C "$local_repo" -xf -
  # Keep the libraries required by OpenROAD's /ORFS-Research/dependencies RUNPATH.
  tar -C "$ORFS_BASE" -cf - "${tool_files[@]}" | tar -C "$workspace/ORFS-Research" -xf -
}

stage_inputs() {
  local seed_file source
  for seed_file in 1_synth.sdc 2_2_floorplan_io.def 2_2_floorplan_io.v; do
    source="$input_dir/preparation/results/$seed_file"
    [[ -s "$source" ]] || fail "Missing prepared seed: $source"
    cp -- "$source" "$local_inputs/preparation/results/$seed_file"
  done
  source="$input_dir/solutions/$solution_id/macro_partition.txt"
  [[ -s "$source" ]] || fail "Missing partition: $source"
  cp -- "$source" "$local_inputs/solutions/$solution_id/macro_partition.txt"
}

run_container() {
  module load apptainer
  setsid apptainer exec --cleanenv \
    --bind "$workspace/ORFS-Research:/ORFS-Research:ro" \
    --bind "$local_repo:/TaiWei-Pin-3D" \
    --bind "$workspace/tmp:/tmp" \
    --pwd /TaiWei-Pin-3D \
    --env "NUM_CORES=$SLURM_CPUS_PER_TASK" \
    --env "OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK" \
    "$CC_IMAGE" bash --noprofile --norc /TaiWei-Pin-3D/cc_script/lib/container.sh \
    "$platform" "$design" "$input_relative" "$tier" "$iterations" "$solution_id" "$variant" \
    > "$workspace/run.log" 2>&1 &
  child_pid=$!
  local flow_exit=0
  wait "$child_pid" || flow_exit=$?
  child_pid=""
  return "$flow_exit"
}

save_outputs() {
  local flow_exit=$? state=FAILED tree
  trap - EXIT
  trap '' USR1 TERM INT
  set +e
  local -a outputs=() archive_args=(-C "$workspace" run.log)
  for tree in results logs objects reports; do
    [[ ! -e "$local_repo/$tree/$platform/$design/$variant" ]] || outputs+=("$tree/$platform/$design/$variant")
  done
  if (( ${#outputs[@]} > 0 )); then
    archive_args+=(-C "$local_repo" "${outputs[@]}")
  fi
  if tar -cf "$result_dir/output.tar.partial" "${archive_args[@]}" && \
     mv -- "$result_dir/output.tar.partial" "$result_dir/output.tar"; then
    :
  elif (( flow_exit == 0 )); then
    flow_exit=1
  fi
  if [[ -s "$local_repo/reports/$platform/$design/$variant/openroad_eval.json" ]]; then
    cp -- "$local_repo/reports/$platform/$design/$variant/openroad_eval.json" \
          "$result_dir/openroad_eval.json" || flow_exit=1
  fi
  if (( flow_exit == 0 )); then state=COMPLETED; fi
  if [[ -n "$interrupted" ]]; then state=INTERRUPTED; fi
  printf '[cc] %s: %s (exit %s); signal=%s; saved to %s\n' \
    "$solution_id" "$state" "$flow_exit" "${interrupted:-none}" "$result_dir"
  exit "$flow_exit"
}

stop_for_signal() {
  interrupted="$1"
  trap '' USR1 TERM INT
  if [[ -n "$child_pid" ]]; then
    local flow_pid="$child_pid" watchdog_pid
    kill -TERM -- "-$flow_pid" 2>/dev/null || true
    # A single deadline timer bounds shutdown without a polling/sleep loop.
    (
      trap - EXIT USR1 TERM INT
      sleep 5
      kill -KILL -- "-$flow_pid" 2>/dev/null || true
    ) >/dev/null 2>&1 &
    watchdog_pid=$!
    wait "$flow_pid" 2>/dev/null || true
    kill -KILL -- "-$flow_pid" 2>/dev/null || true
    kill "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true
    child_pid=""
  fi
  exit 143
}

trap save_outputs EXIT
trap 'stop_for_signal TIME_LIMIT_WARNING' USR1
trap 'stop_for_signal TERMINATED' TERM
trap 'stop_for_signal INTERRUPTED' INT
printf '[cc] %s: input=%s; working in %s; outputs will return to %s\n' \
  "$solution_id" "$input_dir" "$workspace" "$result_dir"
stage_runtime
stage_inputs
run_container
