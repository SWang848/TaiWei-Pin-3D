# Regression: a COVER RAM must remain a clock endpoint during CTS reporting.
# Two BLOCK RAMs and register sinks force the combined-tree reporting path.
# The clocked RAM has data feedback; traversing it as a clock
# forwarding cell causes unbounded recursion in the unpatched tool.
set test_dir [file dirname [file normalize [info script]]]
set repo_dir [file normalize [file join $test_dir ../..]]
set platform_dir [file join $repo_dir platforms nangate45_3D]

foreach lib [list \
  lib_bottom/NangateOpenCellLibrary_typical.bottom.lib \
  lib_upper/NangateOpenCellLibrary_typical.upper.lib \
  lib_bottom/fakeram/fakeram45_256x16.bottom.lib \
  lib_upper/fakeram/fakeram45_256x16.upper.lib] {
  read_liberty [file join $platform_dir $lib]
}
foreach lef [list \
  lef/NangateOpenCellLibrary.tech21.lef \
  lef_bottom/NangateOpenCellLibrary.macro.mod.bottom.lef \
  lef_upper/NangateOpenCellLibrary.macro.mod.upper.cover.lef \
  lef_bottom/fakeram_block/fakeram45_256x16.bottom.lef \
  lef_upper/fakeram_cover/fakeram45_256x16.upper.cover.lef] {
  read_lef [file join $platform_dir $lef]
}
read_def [file join $test_dir cts_cover_memory.def]
create_clock -name core_clock -period 3 [get_ports clk]
source [file join $platform_dir setRC.tech21.tcl]

set block [ord::get_db_block]
set ram [$block findInst covered_ram]
set original_location [$ram getLocation]
set original_orientation [$ram getOrient]
set original_status [$ram getPlacementStatus]
if {[[$ram getMaster] getType] ne "COVER" || [$ram isBlock]} {
  error "Regression fixture must contain a COVER RAM"
}
clock_tree_synthesis -sink_clustering_enable -repair_clock_nets \
  -root_buf CLKBUF_X3_bottom -buf_list CLKBUF_X3_bottom

if {[[$ram getMaster] getType] ne "COVER" || \
    [$ram getLocation] ne $original_location || \
    [$ram getOrient] ne $original_orientation || \
    [$ram getPlacementStatus] ne $original_status} {
  error "CTS changed the covered RAM's physical view or placement"
}
foreach {instance pin expected_net} {
  covered_ram {rd_out[0]} ram_data
  feedback A ram_data
  feedback ZN ram_addr
  covered_ram {addr_in[7]} ram_addr
} {
  set inst [$block findInst $instance]
  set net [[$inst findITerm $pin] getNet]
  if {$net eq "NULL" || $net eq "" || [$net getName] ne $expected_net} {
    error "CTS changed RAM data feedback: $instance/$pin"
  }
}

# Independently follow clock buffers/inverters and count the original loads.
set expected {macro0/clk macro1/clk covered_ram/clk ff0/CK ff1/CK}
array set reached {}
array set visited {}
set queue [list [[$block findBTerm clk] getNet]]
set cursor 0
while {$cursor < [llength $queue]} {
  set net [lindex $queue $cursor]
  incr cursor
  set net_name [$net getName]
  if {[info exists visited($net_name)]} {continue}
  set visited($net_name) 1
  foreach term [$net getITerms] {
    set mterm [$term getMTerm]
    if {[$mterm getIoType] ne "INPUT"} {continue}
    set inst [$term getInst]
    set key "[$inst getName]/[$mterm getName]"
    if {$key in $expected} {
      set reached($key) 1
      continue
    }
    set master_name [[$inst getMaster] getName]
    if {[string match "CLKBUF_*" $master_name] || \
        [string match "BUF_*" $master_name] || \
        [string match "INV_*" $master_name]} {
      set out [$inst getFirstOutput]
      if {$out ne "NULL" && $out ne ""} {
        set next_net [$out getNet]
        if {$next_net ne "NULL" && $next_net ne ""} {lappend queue $next_net}
      }
    }
  }
}
foreach key $expected {
  if {![info exists reached($key)]} {error "Disconnected clock endpoint: $key"}
}
if {[info exists visited(ram_data)] || [info exists visited(ram_addr)]} {
  error "Clock connectivity walk entered the RAM data feedback path"
}
puts "PASS: CTS terminated; all 5 clock endpoints, RAM data feedback, and COVER state are preserved"
