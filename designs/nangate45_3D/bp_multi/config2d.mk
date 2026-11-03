export DESIGN_NAME = bp_multi_top
export DESIGN_NICKNAME = bp_multi
export PLATFORM    = nangate45

export SYNTH_HIERARCHICAL = 1
export ABC_AREA = 1

export VERILOG_FILES = $(DESIGN_HOME)/nangate45_3D/$(DESIGN_NICKNAME)/$(DESIGN_NAME).v

export SDC_FILE      = $(DESIGN_HOME)/nangate45_3D/$(DESIGN_NICKNAME)/constraint.sdc

export ADDITIONAL_LEFS = $(sort $(wildcard $(PLATFORM_DIR)/lef/fakeram/*.lef))
export ADDITIONAL_LIBS = $(sort $(wildcard $(PLATFORM_DIR)/lib/fakeram/*.lib))

export CORE_UTILIZATION ?= 60
export CORE_ASPECT_RATIO = 1
export CORE_MARGIN = 5
export GEN_EFF = medium
export MAP_EFF = high

export NUM_CORES ?= 32
