# Set your CC project path here.
export CC_BASE="/home/shang8/projects/aip-mtaylor3/shang8"
# Optional Slurm allocation. Leave empty to use the cluster's default account.
export CC_ACCOUNT="aip-mtaylor3"

# Fixed paths. Edit individually if the folders are in different locations.
export TAIWEI_BASE="$CC_BASE/TaiWei-Pin-3D"
export ORFS_BASE="$CC_BASE/ORFS-Research"
export CC_IMAGE="$CC_BASE/taiwei.sif"
export CC_INPUT_ROOT="$TAIWEI_BASE/partition_experiments"
export CC_OUTPUT_ROOT="$SCRATCH/taiwei"

# Experiment and resource settings, shared by all jobs.
# Memory and time sized from the bp_be pilot; remeasure for other designs.
export CC_PLATFORM="nangate45_3D"
export CC_CPUS=8
export CC_MEMORY="16G"
export CC_TIME="02:00:00"
export CC_STD_CELL_TIER="bottom"
export CC_OUTER_ITERATIONS=1
