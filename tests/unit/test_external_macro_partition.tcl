# Run with: tclsh tests/unit/test_external_macro_partition.tcl
package require tcltest 2
namespace import ::tcltest::test

set repo_dir [file normalize [file join [file dirname [info script]] ../..]]
set ::env(OPENROAD_SCRIPTS_DIR) [file join $repo_dir scripts_openroad]
set ::env(REPORTS_DIR) [::tcltest::temporaryDirectory]
set ::env(PIN3D_NO_AUTORUN) 1
source [file join $::env(OPENROAD_SCRIPTS_DIR) generate_3d_views.tcl]

proc parse_macro_text {text} {
  set fh [file tempfile path]
  try {
    puts $fh $text
    close $fh
    return [::pin3d::parse_macro_partition_file $path]
  } finally {
    catch {close $fh}
    file delete $path
  }
}

oo::class create MockMacroMaster {
  variable type
  constructor {master_type} {set type $master_type}
  method getType {} {return $type}
}
oo::class create MockMacroInstance {
  variable name master
  constructor {inst_name master_type} {
    set name $inst_name
    set master [MockMacroMaster new $master_type]
  }
  destructor {$master destroy}
  method getName {} {return $name}
  method getMaster {} {return $master}
}

set ram0 [MockMacroInstance new top/ram0 BLOCK]
set ram1 [MockMacroInstance new top/ram1 BLOCK]
set core [MockMacroInstance new top/logic CORE]
set insts [list $ram0 $ram1 $core]
set norm_to_inst [dict create top/ram0 $ram0 top/ram1 $ram1 top/logic $core]
set macro_map [dict create top/ram0 0 top/ram1 1]

test macro-parser-1 {comments, whitespace and escaped instance names} -body {
  parse_macro_text {# physical tiers
top/ram\[0\]    0
// another comment
top/ram1 1}
} -result {{top/ram[0]} 0 top/ram1 1}

test macro-parser-2 {invalid labels are rejected} -body {
  parse_macro_text {top/ram0 2}
} -returnCodes error -match glob -result {*expected*}

test macro-parser-3 {normalization cannot hide duplicate assignments} -body {
  parse_macro_text {top/ram\[0\] 0
top/ram[0] 1}
} -returnCodes error -match glob -result {*duplicate macro partition:*}

test macro-parser-4 {empty files are rejected} -body {
  parse_macro_text {# no assignments}
} -returnCodes error -match glob -result {*Macro partition file is empty:*}

test macro-parser-5 {unexpected columns are rejected} -body {
  parse_macro_text {top/ram0 10 20 0}
} -returnCodes error -match glob -result {*expected*}

test macro-map-1 {macros retain their physical tiers and logic uses bottom} -body {
  set result [::pin3d::complete_macro_partition $macro_map $insts $norm_to_inst bottom]
  list [dict get $result top/ram0] [dict get $result top/ram1] [dict get $result top/logic]
} -result {0 1 1}

test macro-map-2 {upper standard-cell selection does not swap macro labels} -body {
  set result [::pin3d::complete_macro_partition $macro_map $insts $norm_to_inst upper]
  list [dict get $result top/ram0] [dict get $result top/ram1] [dict get $result top/logic]
} -result {0 1 0}

test macro-map-3 {every macro needs an assignment} -body {
  ::pin3d::complete_macro_partition [dict create top/ram0 0] $insts $norm_to_inst bottom
} -returnCodes error -result {Missing macro partition: top/ram1}

test macro-map-4 {flattened external names require explicit conversion} -body {
  ::pin3d::complete_macro_partition [dict create top_ram0 0] $insts $norm_to_inst bottom
} -returnCodes error -match glob -result {Unknown macro instance 'top_ram0';*}

test macro-map-5 {standard cells cannot be included in the macro input} -body {
  ::pin3d::complete_macro_partition [dict create top/logic 0] $insts $norm_to_inst bottom
} -returnCodes error -result {Macro partition entry 'top/logic' is not a BLOCK macro}

test macro-map-6 {invalid standard-cell tiers are rejected} -body {
  ::pin3d::complete_macro_partition $macro_map $insts $norm_to_inst sideways
} -returnCodes error -match glob -result {STD_CELL_TIER must be bottom or upper*}

test macro-map-7 {a COVER seed cannot be silently treated as a standard cell} -body {
  set covered [MockMacroInstance new top/covered COVER]
  try {
    ::pin3d::complete_macro_partition $macro_map [concat $insts [list $covered]] $norm_to_inst bottom
  } finally {
    $covered destroy
  }
} -returnCodes error -match glob -result {Unsupported 2D seed master type 'COVER'*}

set ::env(RESULTS_DIR) [file join [::tcltest::temporaryDirectory] external_macro_seed]
set ::env(PLATFORM_DIR) [file join $repo_dir platforms nangate45_3D]
set ::env(DESIGN_NAME) bp_be_top

test macro-input-1 {legacy mode retains the original partition path} -body {
  set ::env(MACRO_PARTITION_FILE) ""
  set inputs [::pin3d::resolve_run_inputs]
  list [dict get $inputs external_macros] [file tail [dict get $inputs partition_path]]
} -result {0 partition.txt}

test macro-input-2 {external mode reads the selected file directly} -body {
  set ::env(MACRO_PARTITION_FILE) [file join $repo_dir designs nangate45_3D bp_be macro_partition.txt]
  set inputs [::pin3d::resolve_run_inputs]
  list [dict get $inputs external_macros] [expr {[dict get $inputs partition_path] eq $::env(MACRO_PARTITION_FILE)}]
} -result {1 1}

test macro-bp-be-1 {the converted solution preserves both supplied groups} -body {
  set imported [::pin3d::parse_macro_partition_file $::env(MACRO_PARTITION_FILE)]
  set expected [dict create \
    be_mmu/dcache/data_mem_0__data_mem/macro_mem/mem 0 \
    be_mmu/dcache/data_mem_7__data_mem/macro_mem/mem 0 \
    be_mmu/dcache/data_mem_5__data_mem/macro_mem/mem 0 \
    be_mmu/dcache/data_mem_6__data_mem/macro_mem/mem 0 \
    be_mmu/dcache/stat_mem/macro_mem/mem 1 \
    be_mmu/dcache/tag_mem/macro_mem/mem 1 \
    be_mmu/dcache/data_mem_4__data_mem/macro_mem/mem 1 \
    be_mmu/dcache/data_mem_2__data_mem/macro_mem/mem 1 \
    be_mmu/dcache/data_mem_3__data_mem/macro_mem/mem 1 \
    be_mmu/dcache/data_mem_1__data_mem/macro_mem/mem 1]
  expr {$imported eq $expected}
} -result 1

foreach inst $insts {$inst destroy}
set failed $::tcltest::numTests(Failed)
::tcltest::cleanupTests
if {$failed} {exit 1}
