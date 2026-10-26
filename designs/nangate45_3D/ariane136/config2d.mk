export DESIGN_NAME = ariane
export DESIGN_NICKNAME = ariane136
export PLATFORM = nangate45

# Keep the upstream benchmark inputs in the ORFS checkout.
export ORFS_DIR ?= $(FLOW_HOME)/../ORFS-Research
export ORFS_DESIGN_HOME ?= $(abspath $(ORFS_DIR)/flow/designs)
export VERILOG_FILES = $(ORFS_DESIGN_HOME)/src/ariane136/ariane.sv2v.v \
                       $(ORFS_DESIGN_HOME)/nangate45/ariane136/macros.v
export SDC_FILE = $(ORFS_DESIGN_HOME)/nangate45/ariane136/constraint.sdc

export SYNTH_HIERARCHICAL = 1
export ADDITIONAL_LEFS = $(PLATFORM_DIR)/lef/fakeram/fakeram45_256x16.lef
export ADDITIONAL_LIBS = $(PLATFORM_DIR)/lib/fakeram/fakeram45_256x16.lib

# Use the existing Taiwei Ariane floorplan policy. The upstream fixed die
# and edge restrictions are not used by Taiwei's automatic 3D floorplan/IO.
export CORE_UTILIZATION ?= 50
export CORE_ASPECT_RATIO = 1
export CORE_MARGIN = 5
export MACRO_PLACE_HALO = 10 10
export PLACE_DENSITY = 0.35
export TNS_END_PERCENT = 100
export GEN_EFF = medium
export MAP_EFF = high
export NUM_CORES ?= 32
