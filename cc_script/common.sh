#!/usr/bin/env bash
# Host-side helpers. Batch scripts source this from the uploaded repository,
# since Slurm runs its own copy of an sbatch script from a spool directory.

cc_run_container() {
  : "${CC_BASE:?Export CC_BASE to the directory used for the upload.}"
  : "${SLURM_JOB_ID:?Run this script through Slurm.}"
  : "${SLURM_CPUS_PER_TASK:?Request --cpus-per-task in the Slurm job.}"
  local image="${CC_IMAGE:-${CC_BASE}/containers/orfs.sif}"
  local repo="${CC_BASE}/TaiWei-Pin-3D"
  local -a options=(
    --cleanenv
    --bind "${CC_BASE}/ORFS-Research:/ORFS-Research:ro"
    --bind "${repo}:/TaiWei-Pin-3D"
    --pwd /TaiWei-Pin-3D
    --env "NUM_CORES=${SLURM_CPUS_PER_TASK}"
    --env "OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK}"
  )
  if [[ ! -r "$image" ]]; then
    printf 'Cannot read SIF: %s\nConvert the Docker archive once before submitting.\n' "$image" >&2
    return 1
  fi
  if [[ -n "${SLURM_TMPDIR:-}" ]]; then
    options+=(--bind "${SLURM_TMPDIR}:/tmp")
  fi
  module load apptainer
  exec apptainer exec "${options[@]}" "$image" \
    bash --noprofile --norc /TaiWei-Pin-3D/cc_script/container_job.sh "$@"
}
