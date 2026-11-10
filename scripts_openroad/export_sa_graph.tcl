# Standalone OpenDB adapter for generate_sa_macro_partitions.py. No placement,
# partitioning, timing libraries, or DreamPlace Python modules are required.
read_db $::env(SA_SEED_DB)
set block [ord::get_db_block]
if {$block eq "" || $block eq "NULL"} {
  error "No OpenDB block loaded from $::env(SA_SEED_DB)"
}
set dbu [[ord::get_db_tech] getDbUnitsPerMicron]
if {$dbu <= 0} {error "Invalid database units per micron: $dbu"}
set delimiter "/"
if {![catch {set value [$block getHierarchyDelimiter]}] && $value ne ""} {
  set delimiter $value
}
if {[regexp {\s} $delimiter]} {error "Invalid hierarchy delimiter"}

set output $::env(SA_GRAPH_FILE)
file mkdir [file dirname $output]
set fh [open $output w]
try {
  puts $fh "# taiwei-sa-graph-v1"
  puts $fh "hierarchy\t$delimiter"
  set ids [dict create]
  set next_id 0
  foreach inst [$block getInsts] {
    set master [$inst getMaster]
    set name [$inst getName]
    set master_name [$master getName]
    if {$name eq "" || $master_name eq "" || [regexp {\s} $name] || [regexp {\s} $master_name]} {
      error "Whitespace or empty instance/master name cannot be exported: $name"
    }
    set is_macro [expr {[string match "BLOCK*" [$master getType]] ? 1 : 0}]
    set area [expr {(double([$master getWidth]) / $dbu) * (double([$master getHeight]) / $dbu)}]
    dict set ids $inst $next_id
    puts $fh [join [list node $next_id $name $master_name $is_macro [format %.17g $area]] "\t"]
    incr next_id
  }
  foreach net [$block getNets] {
    if {[$net getSigType] in {POWER GROUND}} {continue}
    set iterms [$net getITerms]
    # IO ports affect the original <10-pin filter, but are not graph vertices.
    set pin_count [expr {[llength $iterms] + [llength [$net getBTerms]]}]
    set row [list net $pin_count]
    foreach iterm $iterms {
      lappend row [dict get $ids [$iterm getInst]]
    }
    puts $fh [join $row "\t"]
  }
} finally {
  close $fh
}
puts "INFO: Exported $next_id instances and signal-net pins to $output"
exit
