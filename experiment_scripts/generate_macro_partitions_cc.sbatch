#!/usr/bin/env bash
#SBATCH --job-name=taiwei-macro-partitions
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=02:00:00
#SBATCH --output=macro-partitions-%j.out

# On Vulcan, set CC_BASE to the directory used for the initial upload, then:
# sbatch --account=aip-YOUR_GROUP experiment_scripts/generate_macro_partitions_cc.sbatch
# Optional arguments are forwarded to generate_macro_partitions.py. Candidate
# paths are relative to the mounted TaiWei-Pin-3D root, or absolute inside it.
# The existing containers/orfs.sif is reused; this script never builds an image.

set -euo pipefail

: "${CC_BASE:?Export CC_BASE to the directory used for the upload.}"
: "${SLURM_CPUS_PER_TASK:?Submit this script with sbatch.}"

image="${CC_BASE}/containers/orfs.sif"
if [[ ! -r "$image" ]]; then
  printf 'Cannot read container image: %s\nConvert the uploaded Docker archive to this SIF once before submitting.\n' "$image" >&2
  exit 1
fi

module load apptainer

if (( $# == 0 )); then
  set -- partition_experiments/nangate45_3D/bp_be/candidates.json
fi

exec apptainer exec --cleanenv \
  --bind "${CC_BASE}/ORFS-Research:/ORFS-Research" \
  --bind "${CC_BASE}/TaiWei-Pin-3D:/TaiWei-Pin-3D" \
  --pwd /TaiWei-Pin-3D \
  --env "NUM_CORES=${SLURM_CPUS_PER_TASK}" \
  --env "OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK}" \
  "$image" \
  bash --noprofile --norc -c '
    set -euo pipefail
    export ORFS_DIR=/ORFS-Research
    export OPENROAD_EXE="$ORFS_DIR/tools/install/OpenROAD/bin/openroad"
    export YOSYS_EXE="$ORFS_DIR/tools/install/yosys/bin/yosys"
    export STA_EXE="$ORFS_DIR/tools/install/OpenROAD/bin/sta"
    export PATH="$ORFS_DIR/tools/install/OpenROAD/bin:$ORFS_DIR/tools/install/yosys/bin:$PATH"
    source ./env.sh

    "$OPENROAD_EXE" -version
    "$YOSYS_EXE" -V
    exec python3 generate_macro_partitions.py "$@" --keep-preparation
  ' -- "$@"
