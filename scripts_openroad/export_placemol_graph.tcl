# Standalone OpenDB input adapter for Place-MoL. No placement or partitioning.
read_db $::env(PLACEMOL_SEED_DB)
set block [ord::get_db_block]
if {$block eq "" || $block eq "NULL"} {error "No OpenDB block loaded"}
set dbu [[ord::get_db_tech] getDbUnitsPerMicron]
if {$dbu <= 0} {error "Invalid database units per micron: $dbu"}

set output $::env(PLACEMOL_GRAPH_FILE)
file mkdir [file dirname $output]
set fh [open $output w]
try {
  puts $fh "# taiwei-placemol-graph-v1"
  set ids [dict create]
  set names [dict create]
  set next_id 0
  foreach inst [$block getInsts] {
    set master [$inst getMaster]
    set name [$inst getName]
    set master_name [$master getName]
    if {$name eq "" || $master_name eq "" || [regexp {\s} $name] || [regexp {\s} $master_name]} {
      error "Invalid instance/master name: $name"
    }
    set kind [expr {[string match "BLOCK*" [$master getType]] ? "macro" : "cell"}]
    set width [expr {double([$master getWidth]) / $dbu}]
    set height [expr {double([$master getHeight]) / $dbu}]
    dict set ids $inst $next_id
    dict set names $name 1
    puts $fh [join [list node $next_id $name $master_name $kind [format %.17g $width] [format %.17g $height]] "\t"]
    incr next_id
  }
  # DreamPlace represents top-level IOs as zero-area terminal nodes. They join
  # the merged cell node and contribute to pin/connectivity counts.
  set port_ids [dict create]
  foreach port [$block getBTerms] {
    dict set port_ids $port $next_id
    set name "@io:$next_id"
    while {[dict exists $names $name]} {append name ":"}
    dict set names $name 1
    puts $fh [join [list node $next_id $name __IO__ port 0 0] "\t"]
    incr next_id
  }
  foreach net [$block getNets] {
    if {[$net getSigType] in {POWER GROUND}} {continue}
    set pins [list]
    foreach iterm [$net getITerms] {lappend pins [dict get $ids [$iterm getInst]]}
    foreach port [$net getBTerms] {lappend pins [dict get $port_ids $port]}
    puts $fh [join [concat [list net [llength $pins]] $pins] "\t"]
  }
} finally {
  close $fh
}
puts "INFO: Exported $next_id instances/IOs and all signal/clock nets to $output"
exit
