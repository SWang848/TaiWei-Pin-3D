#!/usr/bin/env bash
#SBATCH --job-name=taiwei-prepare
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=02:00:00
#SBATCH --output=cc-prepare-%j.out

set -euo pipefail
: "${CC_BASE:?Export CC_BASE before submission.}"
source "${CC_BASE}/TaiWei-Pin-3D/cc_script/common.sh"
cc_run_container prepare "$@"
