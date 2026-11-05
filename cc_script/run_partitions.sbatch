#!/usr/bin/env bash
#SBATCH --job-name=taiwei-partition
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=12:00:00
#SBATCH --output=cc-partition-%A_%a.out

set -euo pipefail
: "${CC_BASE:?Export CC_BASE before submission.}"
: "${SLURM_ARRAY_JOB_ID:?Submit this worker as a Slurm array.}"
: "${SLURM_ARRAY_TASK_ID:?Submit this worker as a Slurm array.}"
source "${CC_BASE}/TaiWei-Pin-3D/cc_script/common.sh"
printf -v solution_id 's%04d' "$SLURM_ARRAY_TASK_ID"
variant="cc_${SLURM_ARRAY_JOB_ID}_${solution_id}_r${SLURM_RESTART_COUNT:-0}"
cc_run_container run "$@" "$SLURM_ARRAY_TASK_ID" "$variant"
