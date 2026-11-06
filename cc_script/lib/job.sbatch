#!/usr/bin/env bash
# Evaluate one already-prepared partition. run.sh submits this as an array.
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --job-name=taiwei

set -euo pipefail
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $# == 5 ]] || fail 'Submit using bash cc_script/run.sh CASE PARALLEL_JOBS.'
platform="$1"; design="$2"; run_relative="$3"; tier="$4"; iterations="$5"
[[ "$platform" =~ ^[A-Za-z0-9_-]+$ && "$design" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'Invalid platform or case.'
: "${TAIWEI_BASE:?Submit using cc_script/run.sh.}"
: "${ORFS_BASE:?Set ORFS_BASE in cc_script/config.sh.}"
: "${CC_IMAGE:?Set CC_IMAGE in cc_script/config.sh.}"
: "${SLURM_JOB_ID:?Run this script through Slurm.}"
: "${SLURM_CPUS_PER_TASK:?Missing Slurm CPU allocation.}"
: "${SLURM_TMPDIR:?Node-local storage is required; no shared-filesystem fallback is used.}"
: "${SLURM_ARRAY_TASK_ID:?Missing array index.}"
: "${SLURM_ARRAY_JOB_ID:?Missing array job ID.}"

shared_repo="$(realpath -e "$TAIWEI_BASE")"
shared_run="$(realpath -e "$shared_repo/$run_relative")"
[[ "$shared_run" == "$shared_repo/partition_experiments/$platform/$design/cc_runs/"* ]] || fail 'Invalid run directory.'
image="$CC_IMAGE"
[[ -r "$image" ]] || fail "Cannot read prebuilt SIF: $image"
restart="${SLURM_RESTART_COUNT:-0}"
solution_id="$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$shared_run/solutions.txt")"
[[ "$solution_id" =~ ^s[0-9]+$ ]] || fail 'Array index has no prepared solution.'
variant="cc_${SLURM_ARRAY_JOB_ID}_${solution_id}_r${restart}"
save_dir="$shared_run/results/$solution_id/r$restart"
mkdir -p "$(dirname "$save_dir")"
mkdir "$save_dir"  # A duplicate job must not overwrite saved results.
work="$(mktemp -d "$SLURM_TMPDIR/taiwei.XXXXXX")"
local_repo="$work/TaiWei-Pin-3D"
local_run="$local_repo/$run_relative"
mkdir -p "$local_run" "$work/ORFS-Research" "$work/tmp"
touch "$work/run.log"
child_pid=""
interrupted=""
runtime_files=(Makefile env.sh designs platforms scripts_openroad util test cc_script/lib)

write_status() {
  printf 'state=%s\nexit_code=%s\nsolution=%s\nvariant=%s\nworkspace=%s\ninterrupted=%s\n' \
    "$1" "$2" "$solution_id" "$variant" "$work" "$interrupted" > "$save_dir/status.txt.tmp"
  mv -- "$save_dir/status.txt.tmp" "$save_dir/status.txt"
}

save_outputs() {
  local code=$? state=FAILED
  trap - EXIT
  trap '' USR1 TERM INT
  set +e
  local -a outputs=()
  local tree
  for tree in results logs objects reports; do
    [[ ! -e "$local_repo/$tree/$platform/$design/$variant" ]] || outputs+=("$tree/$platform/$design/$variant")
  done
  tar -C "$local_repo" -cf "$save_dir/output.tar.partial" "${outputs[@]}" -C "$work" run.log
  if [[ $? == 0 ]] && mv -- "$save_dir/output.tar.partial" "$save_dir/output.tar"; then
    :
  elif (( code == 0 )); then
    code=1
  fi
  cp -- "$work/run.log" "$save_dir/run.log" || { (( code != 0 )) || code=1; }

  if [[ -s "$local_repo/reports/$platform/$design/$variant/openroad_eval.json" ]]; then
    cp -- "$local_repo/reports/$platform/$design/$variant/openroad_eval.json" "$save_dir/openroad_eval.json" || code=1
  fi
  if (( code == 0 )); then state=COMPLETED; fi
  if [[ -n "$interrupted" ]]; then state=INTERRUPTED; fi
  write_status "$state" "$code" || { (( code != 0 )) || code=1; }
  printf '[cc] %s: %s (exit %s); saved to %s\n' "$solution_id" "$state" "$code" "$save_dir"
  exit "$code"
}

stop_for_signal() {
  interrupted="$1"
  trap '' USR1 TERM INT
  if [[ -n "$child_pid" ]]; then
    kill -TERM -- "-$child_pid" 2>/dev/null || true
    for attempt in {1..5}; do
      kill -0 -- "-$child_pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL -- "-$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  exit 143
}
trap save_outputs EXIT
trap 'stop_for_signal TIME_LIMIT_WARNING' USR1
trap 'stop_for_signal TERMINATED' TERM
trap 'stop_for_signal INTERRUPTED' INT
write_status RUNNING ''
printf '[cc] %s: working in %s; outputs will return to %s\n' "$solution_id" "$work" "$save_dir"

# Copy runtime inputs and the selected prepared partition to local disk.
tar -C "$shared_repo" -cf - "${runtime_files[@]}" | tar -C "$local_repo" -xf -
tar -C "$ORFS_BASE" -cf - tools/install | tar -C "$work/ORFS-Research" -xf -
tar -C "$local_run" -xf "$shared_run/inputs.tar" preparation/results "solutions/$solution_id"

module load apptainer
container_args=("$platform" "$design" "$run_relative" "$tier" "$iterations" "$solution_id" "$variant")
setsid apptainer exec --cleanenv \
  --bind "$work/ORFS-Research:/ORFS-Research:ro" \
  --bind "$local_repo:/TaiWei-Pin-3D" \
  --bind "$work/tmp:/tmp" \
  --pwd /TaiWei-Pin-3D \
  --env "NUM_CORES=$SLURM_CPUS_PER_TASK" \
  --env "OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK" \
  "$image" bash --noprofile --norc /TaiWei-Pin-3D/cc_script/lib/container.sh \
  "${container_args[@]}" > "$work/run.log" 2>&1 &
child_pid=$!
set +e
wait "$child_pid"
code=$?
set -e
child_pid=""
exit "$code"
