export DESIGN_NAME = ariane
export DESIGN_NICKNAME = ariane136
export PLATFORM = nangate45

# Keep all benchmark inputs in this case directory, as for ariane133.
export VERILOG_FILES = $(DESIGN_HOME)/nangate45_3D/$(DESIGN_NICKNAME)/ariane.v \
                       $(DESIGN_HOME)/nangate45_3D/$(DESIGN_NICKNAME)/macros.v
export SDC_FILE = $(DESIGN_HOME)/nangate45_3D/$(DESIGN_NICKNAME)/constraint.sdc

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
