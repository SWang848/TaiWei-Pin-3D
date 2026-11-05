# Export exact macro instance/master names from the same 2D seed used by the
# partition flow. No partition file or previous 3D run is needed.
set seed_path [file join $::env(RESULTS_DIR) 2_2_floorplan_io.odb]
if {![file isfile $seed_path]} {
  error "2D seed database not found: $seed_path"
}
read_db $seed_path
set block [ord::get_db_block]
if {$block eq "" || $block eq "NULL"} {
  error "No OpenDB block loaded from $seed_path"
}

set macros [dict create]
foreach inst [$block getInsts] {
  set master [$inst getMaster]
  if {![string match "BLOCK*" [$master getType]]} {continue}
  set name [$inst getName]
  set master_name [$master getName]
  if {[regexp {\s} $name] || [regexp {\s} $master_name]} {
    error "Whitespace in a macro instance/master name cannot be exported: $name"
  }
  if {[dict exists $macros $name]} {
    error "Duplicate loaded macro instance: $name"
  }
  dict set macros $name $master_name
}
if {[dict size $macros] == 0} {
  error "No BLOCK macros found in the loaded 2D seed: $seed_path"
}

set output $::env(MACRO_NAMES_FILE)
file mkdir [file dirname $output]
set fh [open $output w]
try {
  puts $fh "instance_name\tmaster_name"
  foreach name [lsort [dict keys $macros]] {
    puts $fh "$name\t[dict get $macros $name]"
  }
} finally {
  close $fh
}
puts "INFO: Exported [dict size $macros] loaded macro names to $output"
exit
