# ============================================================
# generate_3d_views.tcl
# Generate 3D DEF/Verilog views natively inside OpenROAD/ODB.
# ============================================================

source $::env(OPENROAD_SCRIPTS_DIR)/load.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/util.tcl
source $::env(OPENROAD_SCRIPTS_DIR)/json_lite.tcl

namespace eval ::pin3d {
  # Temporary names used while recreating instances and reconnecting nets.
  variable tmp_inst_idx 0
}

# ----------------------------------------------------------------------
# Logging and naming helpers
# ----------------------------------------------------------------------

proc ::pin3d::log {msg} {
  puts "INFO(PIN3D): $msg"
}

proc ::pin3d::warn {msg} {
  puts "WARN(PIN3D): $msg"
}

proc ::pin3d::normalize_name {name} {
  set t [string trim $name]
  if {[string index $t 0] eq "\\"} {
    set t [string range $t 1 end]
  }
  set t [string map [list {\/} {/} {\[} {[} {\]} {]}] $t]
  return $t
}

proc ::pin3d::strip_tier_suffix {master_name} {
  if {[string match "*_upper" $master_name]} {
    return [string range $master_name 0 end-6]
  }
  if {[string match "*_bottom" $master_name]} {
    return [string range $master_name 0 end-7]
  }
  return $master_name
}

# Deduplicate and normalize mixed 2D/3D LEF/LIB path lists before handing
# them to OpenROAD.
proc ::pin3d::normalize_path_list {paths} {
  set out {}
  set seen [dict create]
  foreach path $paths {
    if {$path eq ""} {
      continue
    }
    if {![file exists $path]} {
      continue
    }
    set norm [file normalize $path]
    if {[dict exists $seen $norm]} {
      continue
    }
    dict set seen $norm 1
    lappend out $norm
  }
  return $out
}

# Small recursive walker used to collect platform LEF/LIB payloads.
proc ::pin3d::collect_files_recursive {root pattern} {
  set out {}
  if {![file exists $root] || ![file isdirectory $root]} {
    return $out
  }
  foreach path [glob -nocomplain -directory $root *] {
    if {[file isdirectory $path]} {
      set out [concat $out [::pin3d::collect_files_recursive $path $pattern]]
    } elseif {[string match $pattern [file tail $path]]} {
      lappend out [file normalize $path]
    }
  }
  return $out
}

# ----------------------------------------------------------------------
# Mixed 2D/3D library view construction
# ----------------------------------------------------------------------

proc ::pin3d::derive_2d_platform_dir {} {
  if {![info exists ::env(PLATFORM)] || $::env(PLATFORM) eq ""} {
    utl::error PIN3D 100 "PLATFORM is not set."
  }
  if {![string match "*_3D" $::env(PLATFORM)]} {
    utl::error PIN3D 101 "PLATFORM '$::env(PLATFORM)' must end with '_3D'."
  }
  set platform_2d [string range $::env(PLATFORM) 0 end-3]
  set platform_root [file dirname [file normalize $::env(PLATFORM_DIR)]]
  set platform_dir_2d [file join $platform_root $platform_2d]
  if {![file isdirectory $platform_dir_2d]} {
    utl::error PIN3D 102 "Derived 2D platform directory not found: $platform_dir_2d"
  }
  return [file normalize $platform_dir_2d]
}

proc ::pin3d::collect_2d_lefs {platform_dir_2d} {
  set lef_root [file join $platform_dir_2d lef]
  set out {}
  foreach lef [::pin3d::collect_files_recursive $lef_root *.lef] {
    set tail [file tail $lef]
    if {[string match "*tech*.lef" $tail]} {
      continue
    }
    if {[string match "*.rect.lef" $tail]} {
      continue
    }
    if {[string match "*.macro.lef" $tail]} {
      set mod_variant [file join [file dirname $lef] [string map [list ".macro.lef" ".macro.mod.lef"] $tail]]
      if {[file exists $mod_variant]} {
        continue
      }
    }
    lappend out $lef
  }
  return [::pin3d::normalize_path_list $out]
}

proc ::pin3d::collect_2d_libs {platform_dir_2d} {
  set lib_root [file join $platform_dir_2d lib]
  return [::pin3d::normalize_path_list [::pin3d::collect_files_recursive $lib_root *.lib]]
}

proc ::pin3d::configure_mixed_library_view {} {
  # The 3D platform carries tier-suffixed stdcells, but the 2D seed design may
  # still reference unsuffixed masters. Load both views so either seed format
  # can be reopened inside one ODB session.
  set platform_dir_2d [::pin3d::derive_2d_platform_dir]
  set lefs_2d [::pin3d::collect_2d_lefs $platform_dir_2d]
  set libs_2d [::pin3d::collect_2d_libs $platform_dir_2d]

  if {![info exists ::env(LEF_FILES)] || $::env(LEF_FILES) eq ""} {
    utl::error PIN3D 103 "LEF_FILES is empty."
  }
  if {![info exists ::env(LIB_FILES)] || $::env(LIB_FILES) eq ""} {
    utl::error PIN3D 104 "LIB_FILES is empty."
  }

  set ::env(LEF_FILES) [::pin3d::normalize_path_list [concat $::env(LEF_FILES) $lefs_2d]]
  set ::env(LIB_FILES) [::pin3d::normalize_path_list [concat $::env(LIB_FILES) $libs_2d]]

  ::pin3d::log "Derived 2D platform: $platform_dir_2d"
  ::pin3d::log "Mixed LEF count=[llength $::env(LEF_FILES)] LIB count=[llength $::env(LIB_FILES)]"
}

# ----------------------------------------------------------------------
# Input parsing
# ----------------------------------------------------------------------

proc ::pin3d::parse_partition_file {path} {
  if {$path eq "" || ![file exists $path]} {
    utl::error PIN3D 105 "partition file not found: $path"
  }
  set fh [open $path r]
  set part_map [dict create]
  while {[gets $fh line] >= 0} {
    set s [string trim $line]
    if {$s eq "" || [string match "#*" $s] || [string match "//*" $s]} {
      continue
    }
    set toks [split $s]
    if {[llength $toks] < 2} {
      continue
    }
    set die_s [lindex $toks end]
    if {$die_s ni {"0" "1"}} {
      continue
    }
    dict set part_map [::pin3d::normalize_name [lindex $toks 0]] $die_s
  }
  close $fh
  if {[dict size $part_map] == 0} {
    utl::error PIN3D 106 "partition file is empty or invalid: $path"
  }
  return $part_map
}

# External input uses exact instance names and physical tiers, not cluster IDs
# whose orientation may be changed later. Reject malformed or duplicate rows.
proc ::pin3d::parse_macro_partition_file {path} {
  if {![file isfile $path]} {
    error "Macro partition file not found: $path"
  }
  set fh [open $path r]
  set part_map [dict create]
  set line_no 0
  try {
    while {[gets $fh line] >= 0} {
      incr line_no
      set s [string trim $line]
      if {$s eq "" || [string match "#*" $s] || [string match "//*" $s]} {
        continue
      }
      set toks [regexp -all -inline {\S+} $s]
      if {[llength $toks] != 2 || [lindex $toks 1] ni {0 1}} {
        error "$path:$line_no: expected '<macro_instance> <0=upper|1=bottom>'"
      }
      set name [::pin3d::normalize_name [lindex $toks 0]]
      if {[dict exists $part_map $name]} {
        error "$path:$line_no: duplicate macro partition: $name"
      }
      dict set part_map $name [lindex $toks 1]
    }
  } finally {
    close $fh
  }
  if {[dict size $part_map] == 0} {
    error "Macro partition file is empty: $path"
  }
  return $part_map
}

proc ::pin3d::is_power_pin_name {pin_name} {
  expr {[string toupper $pin_name] in {"VDD" "VSS" "VPWR" "VGND" "VPB" "VNB" "VNW"}}
}

# Keep the parsed cell map in a single dict so the later flow can pass one
# object around instead of many parallel arrays.
proc ::pin3d::empty_cell_map {} {
  return [dict create \
    base_to_bottom [dict create] \
    base_to_upper [dict create] \
    base_to_bottom_pin_map [dict create] \
    base_to_upper_pin_map [dict create] \
    base_to_bottom_const_pins [dict create] \
    base_to_upper_const_pins [dict create] \
    base_to_tier_areas [dict create] \
    has_heterogeneous_area_map 0 \
    has_cell_map 0]
}

proc ::pin3d::require_dict {value context} {
  if {[catch {dict size $value}]} {
    utl::error PIN3D 117 "cell map format error: '$context' must be an object."
  }
  return $value
}

proc ::pin3d::require_string {value context} {
  if {$value eq "" || $value eq "null"} {
    utl::error PIN3D 118 "cell map format error: '$context' must be a non-empty string."
  }
  return $value
}

proc ::pin3d::require_pin_map {value context} {
  set mapping [::pin3d::require_dict $value $context]
  dict for {src dst} $mapping {
    if {$src eq "" || $src eq "null" || $dst eq "" || $dst eq "null"} {
      utl::error PIN3D 119 "cell map format error: '$context' entries must be non-empty strings."
    }
  }
  return $mapping
}

proc ::pin3d::parse_const_pins {tier_data context} {
  if {![dict exists $tier_data const_pins]} {
    return [dict create]
  }
  set const_pins [::pin3d::require_dict [dict get $tier_data const_pins] $context]
  dict for {pin value} $const_pins {
    if {$pin eq "" || $pin eq "null" || $value eq "" || $value eq "null"} {
      utl::error PIN3D 120 "cell map format error: '$context' entries must be non-empty strings."
    }
  }
  return $const_pins
}

proc ::pin3d::parse_cell_map {path} {
  if {$path eq ""} {
    ::pin3d::warn "cell map JSON is not set, skip explicit mapping and use suffix fallback."
    return [::pin3d::empty_cell_map]
  }
  if {![file exists $path]} {
    ::pin3d::warn "cell map JSON '$path' not found, skip explicit mapping and use suffix fallback."
    return [::pin3d::empty_cell_map]
  }
  if {[catch {set data [::json_lite::parse_file $path]} err]} {
    utl::error PIN3D 107 "cell map JSON '$path' parse failed: $err"
  }
  if {![::json_lite::dict_exists_path $data cells]} {
    utl::error PIN3D 108 "cell map JSON '$path' missing required 'cells' object."
  }

  set base_to_bottom [dict create]
  set base_to_upper [dict create]
  set base_to_bottom_pin_map [dict create]
  set base_to_upper_pin_map [dict create]
  set base_to_bottom_const_pins [dict create]
  set base_to_upper_const_pins [dict create]
  set base_to_tier_areas [dict create]
  set has_heterogeneous_area_map 0

  set cells [::pin3d::require_dict [dict get $data cells] cells]
  dict for {cell_key cell_data} $cells {
    set cell_data [::pin3d::require_dict $cell_data "cells.$cell_key"]
    set base_data [::pin3d::require_dict [::json_lite::dict_get_default $cell_data base ""] "cells.$cell_key.base"]
    set bottom [::pin3d::require_dict [::json_lite::dict_get_default $cell_data bottom ""] "cells.$cell_key.bottom"]
    set upper [::pin3d::require_dict [::json_lite::dict_get_default $cell_data upper ""] "cells.$cell_key.upper"]

    set base [::pin3d::require_string [::json_lite::dict_get_default $base_data macro ""] "cells.$cell_key.base.macro"]
    set bottom_macro [::json_lite::dict_get_default $bottom macro ""]
    set upper_macro [::json_lite::dict_get_default $upper macro ""]
    set bottom_macro [::pin3d::require_string $bottom_macro "cells.$cell_key.bottom.macro"]
    set upper_macro [::pin3d::require_string $upper_macro "cells.$cell_key.upper.macro"]
    dict set base_to_bottom $base $bottom_macro
    dict set base_to_upper $base $upper_macro
    dict set base_to_bottom_pin_map $base [::pin3d::require_pin_map \
      [::json_lite::dict_get_default $bottom pin_map ""] "cells.$cell_key.bottom.pin_map"]
    dict set base_to_upper_pin_map $base [::pin3d::require_pin_map \
      [::json_lite::dict_get_default $upper pin_map ""] "cells.$cell_key.upper.pin_map"]

    set bottom_const_pins [::pin3d::parse_const_pins $bottom "cells.$cell_key.bottom.const_pins"]
    if {[dict size $bottom_const_pins] > 0} {
      dict set base_to_bottom_const_pins $base $bottom_const_pins
    }
    set upper_const_pins [::pin3d::parse_const_pins $upper "cells.$cell_key.upper.const_pins"]
    if {[dict size $upper_const_pins] > 0} {
      dict set base_to_upper_const_pins $base $upper_const_pins
    }

    set bw [::json_lite::try_double [::json_lite::dict_get_default $bottom width ""] ""]
    set bh [::json_lite::try_double [::json_lite::dict_get_default $bottom height ""] ""]
    set uw [::json_lite::try_double [::json_lite::dict_get_default $upper width ""] ""]
    set uh [::json_lite::try_double [::json_lite::dict_get_default $upper height ""] ""]
    if {$bw ne "" && $bh ne "" && $uw ne "" && $uh ne ""} {
      set bottom_area [expr {$bw * $bh}]
      set upper_area [expr {$uw * $uh}]
      dict set base_to_tier_areas $base [list $bottom_area $upper_area]
      if {$bottom_macro ne $upper_macro || abs($bottom_area - $upper_area) > 1.0e-12} {
        set has_heterogeneous_area_map 1
      }
    }
  }

  return [dict create \
    base_to_bottom $base_to_bottom \
    base_to_upper $base_to_upper \
    base_to_bottom_pin_map $base_to_bottom_pin_map \
    base_to_upper_pin_map $base_to_upper_pin_map \
    base_to_bottom_const_pins $base_to_bottom_const_pins \
    base_to_upper_const_pins $base_to_upper_const_pins \
    base_to_tier_areas $base_to_tier_areas \
    has_heterogeneous_area_map $has_heterogeneous_area_map \
    has_cell_map 1]
}

# ----------------------------------------------------------------------
# ODB inspection and partition orientation
# ----------------------------------------------------------------------

proc ::pin3d::collect_block_context {block} {
  set insts {}
  set norm_to_inst [dict create]
  set norm_to_base [dict create]
  foreach inst [::odb::dbBlock_getInsts $block] {
    lappend insts $inst
    set inst_name [$inst getName]
    set inst_norm [::pin3d::normalize_name $inst_name]
    set master_name [[ $inst getMaster ] getName]
    dict set norm_to_inst $inst_norm $inst
    dict set norm_to_base $inst_norm [::pin3d::strip_tier_suffix $master_name]
  }
  set top_pins [dict create]
  foreach bterm [::odb::dbBlock_getBTerms $block] {
    dict set top_pins [::pin3d::normalize_name [$bterm getName]] 1
  }
  return [dict create \
    insts $insts \
    norm_to_inst $norm_to_inst \
    norm_to_base $norm_to_base \
    top_pins $top_pins]
}

# Complete a macro-only map with standard-cell assignments. Validate against
# the loaded 2D seed so typos cannot silently leave instances unconverted.
proc ::pin3d::complete_macro_partition {part_map insts norm_to_inst std_tier} {
  if {$std_tier ni {bottom upper}} {
    error "STD_CELL_TIER must be bottom or upper (got '$std_tier')"
  }
  set std_die [expr {$std_tier eq "upper" ? 0 : 1}]
  dict for {name die} $part_map {
    if {![dict exists $norm_to_inst $name]} {
      error "Unknown macro instance '$name'; use exact names from the 2D seed design"
    }
    set inst [dict get $norm_to_inst $name]
    if {![string match "BLOCK*" [[$inst getMaster] getType]]} {
      error "Macro partition entry '$name' is not a BLOCK macro"
    }
    if {$die ni {0 1}} {
      error "Invalid physical tier for macro '$name': $die"
    }
  }

  set completed [dict create]
  set macro_count 0
  set std_count 0
  foreach inst $insts {
    set name [::pin3d::normalize_name [$inst getName]]
    set type [[$inst getMaster] getType]
    if {[string match "BLOCK*" $type]} {
      if {![dict exists $part_map $name]} {
        error "Missing macro partition: $name"
      }
      dict set completed $name [dict get $part_map $name]
      incr macro_count
    } elseif {[string match "CORE*" $type]} {
      dict set completed $name $std_die
      incr std_count
    } else {
      error "Unsupported 2D seed master type '$type' for '$name'; expected BLOCK* or CORE*"
    }
  }
  ::pin3d::log "External macro partition: macros=$macro_count standard_cells=$std_count standard_cell_tier=$std_tier; keep physical labels 0=upper, 1=bottom"
  return $completed
}

proc ::pin3d::swap_partition_labels {part_map} {
  set swapped [dict create]
  dict for {name die} $part_map {
    if {$die ni {"0" "1"}} {
      dict set swapped $name $die
    } elseif {$die eq "0"} {
      dict set swapped $name 1
    } else {
      dict set swapped $name 0
    }
  }
  return $swapped
}

proc ::pin3d::choose_partition_orientation_by_area {part_map norm_to_base base_to_tier_areas has_heterogeneous_area_map} {
  if {[dict size $part_map] == 0 || !$has_heterogeneous_area_map || [dict size $base_to_tier_areas] == 0} {
    ::pin3d::log "Keep original partition labels: no usable heterogeneous map-based area data."
    return $part_map
  }

  set original_upper_area 0.0
  set original_bottom_area 0.0
  set swapped_upper_area 0.0
  set swapped_bottom_area 0.0
  set mapped_count 0
  set skipped_count 0

  dict for {name die} $part_map {
    if {$die ni {"0" "1"}} {
      incr skipped_count
      continue
    }
    if {![dict exists $norm_to_base $name]} {
      incr skipped_count
      continue
    }
    set base [dict get $norm_to_base $name]
    if {![dict exists $base_to_tier_areas $base]} {
      incr skipped_count
      continue
    }
    lassign [dict get $base_to_tier_areas $base] bottom_area upper_area
    incr mapped_count

    if {$die eq "0"} {
      set original_upper_area [expr {$original_upper_area + $upper_area}]
      set swapped_bottom_area [expr {$swapped_bottom_area + $bottom_area}]
    } else {
      set original_bottom_area [expr {$original_bottom_area + $bottom_area}]
      set swapped_upper_area [expr {$swapped_upper_area + $upper_area}]
    }
  }

  if {$mapped_count == 0} {
    ::pin3d::log "Keep original partition labels: no instances matched usable map-based area data."
    return $part_map
  }

  set original_max_area [expr {($original_upper_area > $original_bottom_area) ? $original_upper_area : $original_bottom_area}]
  set swapped_max_area [expr {($swapped_upper_area > $swapped_bottom_area) ? $swapped_upper_area : $swapped_bottom_area}]
  ::pin3d::log [format "Partition orientation area estimate: mapped=%d skipped=%d; original upper=%.6f bottom=%.6f max=%.6f; swapped upper=%.6f bottom=%.6f max=%.6f" \
    $mapped_count $skipped_count $original_upper_area $original_bottom_area $original_max_area \
    $swapped_upper_area $swapped_bottom_area $swapped_max_area]

  if {$swapped_max_area + 1.0e-12 < $original_max_area} {
    ::pin3d::log "Selected swapped partition orientation: label 0 -> bottom, label 1 -> upper."
    return [::pin3d::swap_partition_labels $part_map]
  }

  ::pin3d::log "Selected original partition orientation: label 0 -> upper, label 1 -> bottom."
  return $part_map
}

# Homogeneous libraries cannot use tier-area estimates, so fall back to the
# Python-compatible "pin-heavier cluster stays on bottom" heuristic.
proc ::pin3d::choose_partition_orientation_by_pins {part_map top_pins} {
  if {[dict size $part_map] == 0 || [dict size $top_pins] == 0} {
    ::pin3d::log "Keep original partition labels: no DEF top-level pins found for homogeneous pin-based orientation."
    return $part_map
  }

  set pin_count_0 0
  set pin_count_1 0
  set skipped_count 0
  dict for {name die} $part_map {
    if {![dict exists $top_pins $name]} {
      continue
    }
    if {$die eq "0"} {
      incr pin_count_0
    } elseif {$die eq "1"} {
      incr pin_count_1
    } else {
      incr skipped_count
    }
  }

  set total_pins [expr {$pin_count_0 + $pin_count_1}]
  if {$total_pins == 0} {
    ::pin3d::log "Keep original partition labels: no partitioned top-level pins matched DEF PINS."
    return $part_map
  }

  ::pin3d::log "Homogeneous pin-based orientation estimate: partition0_pins=$pin_count_0 partition1_pins=$pin_count_1 skipped=$skipped_count"
  if {$pin_count_0 > $pin_count_1} {
    ::pin3d::log "Selected swapped partition orientation: pin-heavier cluster moved to bottom tier."
    return [::pin3d::swap_partition_labels $part_map]
  }
  if {$pin_count_1 > $pin_count_0} {
    ::pin3d::log "Selected original partition orientation: pin-heavier cluster already on bottom tier."
  } else {
    ::pin3d::log "Keep original partition labels: homogeneous pin counts tie."
  }
  return $part_map
}

proc ::pin3d::choose_partition_orientation {part_map cell_map norm_to_base top_pins} {
  set adjusted_part_map [::pin3d::choose_partition_orientation_by_area \
    $part_map \
    $norm_to_base \
    [dict get $cell_map base_to_tier_areas] \
    [dict get $cell_map has_heterogeneous_area_map]]

  if {![dict get $cell_map has_heterogeneous_area_map] || [dict size [dict get $cell_map base_to_tier_areas]] == 0} {
    set adjusted_part_map [::pin3d::choose_partition_orientation_by_pins $adjusted_part_map $top_pins]
  }
  return $adjusted_part_map
}

# ----------------------------------------------------------------------
# Instance remapping and reconnection
# ----------------------------------------------------------------------

proc ::pin3d::target_master_name {base die cell_map stats_var} {
  upvar 1 $stats_var stats
  if {![dict get $cell_map has_cell_map]} {
    dict incr stats fallback_count
    if {$die eq "0"} {
      return "${base}_upper"
    }
    return "${base}_bottom"
  }
  if {$die eq "0"} {
    set explicit [::json_lite::dict_get_default [dict get $cell_map base_to_upper] $base ""]
    if {$explicit ne ""} {
      return $explicit
    }
    utl::error PIN3D 121 "cell map missing upper.macro for base cell '$base'."
  }
  set explicit [::json_lite::dict_get_default [dict get $cell_map base_to_bottom] $base ""]
  if {$explicit ne ""} {
    return $explicit
  }
  utl::error PIN3D 122 "cell map missing bottom.macro for base cell '$base'."
}

proc ::pin3d::target_pin_name {base die old_pin cell_map inst_name} {
  if {![dict get $cell_map has_cell_map]} {
    return $old_pin
  }
  if {$die eq "0"} {
    set pin_maps [dict get $cell_map base_to_upper_pin_map]
    set tier upper
  } else {
    set pin_maps [dict get $cell_map base_to_bottom_pin_map]
    set tier bottom
  }
  if {![dict exists $pin_maps $base]} {
    utl::error PIN3D 123 "cell map missing $tier.pin_map for base cell '$base'."
  }
  set pin_map [dict get $pin_maps $base]
  if {[dict exists $pin_map $old_pin]} {
    return [dict get $pin_map $old_pin]
  }
  if {[regexp {^(.+)(\[[^\]]+\])$} $old_pin -> bus_base bus_index] && [dict exists $pin_map $bus_base]} {
    return "[dict get $pin_map $bus_base]$bus_index"
  }
  utl::error PIN3D 124 "cell map missing $tier pin mapping for base cell '$base' pin '$old_pin' on instance '$inst_name'."
}

proc ::pin3d::tier_const_pins {base die cell_map} {
  if {![dict get $cell_map has_cell_map]} {
    return [dict create]
  }
  if {$die eq "0"} {
    return [::json_lite::dict_get_default [dict get $cell_map base_to_upper_const_pins] $base [dict create]]
  }
  return [::json_lite::dict_get_default [dict get $cell_map base_to_bottom_const_pins] $base [dict create]]
}

proc ::pin3d::record_instance_const_pins {const_bindings_var inst_name const_pins connected_targets} {
  upvar 1 $const_bindings_var const_bindings
  if {[dict size $const_pins] == 0} {
    return
  }
  set missing [dict create]
  dict for {pin value} $const_pins {
    if {![dict exists $connected_targets $pin]} {
      dict set missing $pin $value
    }
  }
  if {[dict size $missing] > 0} {
    dict set const_bindings [::pin3d::normalize_name $inst_name] $missing
  }
}

proc ::pin3d::index_instance_iterms {inst} {
  set by_name [dict create]
  foreach iterm [$inst getITerms] {
    dict set by_name [[$iterm getMTerm] getName] $iterm
  }
  return $by_name
}

proc ::pin3d::lookup_target_master {db target_master_name cache_var} {
  upvar 1 $cache_var cache
  if {![dict exists $cache $target_master_name]} {
    set target_master [::odb::dbDatabase_findMaster $db $target_master_name]
    dict set cache $target_master_name $target_master
  }
  return [dict get $cache $target_master_name]
}

proc ::pin3d::replace_partitioned_instances {block db insts part_map cell_map norm_to_inst} {
  set stats [dict create upper_count 0 bottom_count 0 fallback_count 0 ignored_partition_names 0 total_partition_names [dict size $part_map]]
  set const_bindings [dict create]
  set target_master_cache [dict create]

  set ignored 0
  dict for {name die} $part_map {
    if {![dict exists $norm_to_inst $name]} {
      incr ignored
    }
  }
  dict set stats ignored_partition_names $ignored

  dict set stats total_instances [llength $insts]
  dict set stats replaced_instances 0

  foreach inst $insts {
    set inst_name [$inst getName]
    set inst_norm [::pin3d::normalize_name $inst_name]
    if {![dict exists $part_map $inst_norm]} {
      continue
    }
    set die [dict get $part_map $inst_norm]
    if {$die ni {"0" "1"}} {
      continue
    }

    set old_master [$inst getMaster]
    set old_master_name [$old_master getName]
    set base [::pin3d::strip_tier_suffix $old_master_name]
    set target_master_name [::pin3d::target_master_name $base $die $cell_map stats]
    set target_master [::pin3d::lookup_target_master $db $target_master_name target_master_cache]
    if {$target_master eq "" || $target_master eq "NULL"} {
      utl::error PIN3D 109 "Target master '$target_master_name' not found for instance '$inst_name'."
    }

    set pin_to_net [dict create]
    foreach iterm [$inst getITerms] {
      set net [$iterm getNet]
      if {$net eq "" || $net eq "NULL"} {
        continue
      }
      set pin_name [[$iterm getMTerm] getName]
      dict set pin_to_net $pin_name $net
    }

    set loc [$inst getLocation]
    set orient [$inst getOrient]
    set placement_status [$inst getPlacementStatus]
    set source_type [::odb::dbInst_getSourceType $inst]
    set region [::odb::dbInst_getRegion $inst]
    set group [::odb::dbInst_getGroup $inst]

    variable tmp_inst_idx
    incr tmp_inst_idx
    set tmp_name [format "__pin3d_tmp_%d__" $tmp_inst_idx]
    set new_inst [::odb::dbInst_create $block $target_master $tmp_name]
    if {$new_inst eq "" || $new_inst eq "NULL"} {
      utl::error PIN3D 110 "Failed to create replacement instance for '$inst_name'."
    }

    if {[llength $loc] == 2} {
      ::odb::dbInst_setLocation $new_inst [lindex $loc 0] [lindex $loc 1]
    }
    catch {::odb::dbInst_setOrient $new_inst $orient}
    catch {::odb::dbInst_setPlacementStatus $new_inst $placement_status}
    catch {::odb::dbInst_setSourceType $new_inst $source_type}
    if {$region ne "" && $region ne "NULL"} {
      catch {::odb::dbRegion_addInst $region $new_inst}
    }
    if {$group ne "" && $group ne "NULL"} {
      catch {::odb::dbGroup_addInst $group $new_inst}
    }

    set connected_targets [dict create]
    set const_pins [::pin3d::tier_const_pins $base $die $cell_map]
    set new_iterms [::pin3d::index_instance_iterms $new_inst]
    dict for {old_pin net} $pin_to_net {
      set new_pin [::pin3d::target_pin_name $base $die $old_pin $cell_map $inst_name]
      if {[dict exists $connected_targets $new_pin]} {
        utl::error PIN3D 111 "Duplicate target pin '$new_pin' while reconnecting '$inst_name'."
      }
      if {![dict exists $new_iterms $new_pin]} {
        utl::error PIN3D 112 "Target pin '$new_pin' not found on master '$target_master_name' for instance '$inst_name'."
      }
      set new_iterm [dict get $new_iterms $new_pin]
      $new_iterm connect $net
      dict set connected_targets $new_pin 1
    }
    ::pin3d::record_instance_const_pins const_bindings $inst_name $const_pins $connected_targets

    if {$die eq "0"} {
      dict incr stats upper_count
    } else {
      dict incr stats bottom_count
    }

    ::odb::dbInst_destroy $inst
    $new_inst rename $inst_name
    dict incr stats replaced_instances
  }

  dict set stats extra_pin_injection_count [dict size $const_bindings]
  return [dict create stats $stats const_bindings $const_bindings]
}

# ----------------------------------------------------------------------
# Verilog post-processing
# ----------------------------------------------------------------------

proc ::pin3d::split_verilog_statements {text} {
  set spans {}
  set depth 0
  set in_str 0
  set esc 0
  set start 0
  set n [string length $text]
  for {set i 0} {$i < $n} {incr i} {
    set ch [string index $text $i]
    if {$in_str} {
      if {$esc} {
        set esc 0
      } elseif {$ch eq "\\"} {
        set esc 1
      } elseif {$ch eq "\""} {
        set in_str 0
      }
      continue
    }
    switch -- $ch {
      "\"" { set in_str 1 }
      "("  { incr depth }
      ")"  { if {$depth > 0} { incr depth -1 } }
      ";"  {
        if {$depth == 0} {
          lappend spans [list $start [expr {$i + 1}]]
          set start [expr {$i + 1}]
        }
      }
    }
  }
  if {$start < $n} {
    lappend spans [list $start $n]
  }
  return $spans
}

proc ::pin3d::append_const_ports_instance {stmt const_pins} {
  if {[dict size $const_pins] == 0} {
    return $stmt
  }

  set existing {}
  set search_idx 0
  while {[regexp -indices -start $search_idx {\.\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\(} $stmt match_idx pin_idx]} {
    lappend existing [string range $stmt [lindex $pin_idx 0] [lindex $pin_idx 1]]
    set search_idx [expr {[lindex $match_idx 1] + 1}]
  }

  set missing [dict create]
  dict for {pin value} $const_pins {
    if {[lsearch -exact $existing $pin] < 0} {
      dict set missing $pin $value
    }
  }
  if {[dict size $missing] == 0} {
    return $stmt
  }

  set pos [string last ");" $stmt]
  if {$pos < 0} {
    return $stmt
  }

  set prefix [string range $stmt 0 [expr {$pos - 1}]]
  set line_start [expr {[string last "\n" $prefix] + 1}]
  set indent "  "
  if {$line_start >= 0 && $line_start < [string length $prefix]} {
    set tail [string range $prefix $line_start end]
    if {[regexp {^(\s*)} $tail -> spaces]} {
      set indent $spaces
    }
  }

  set insert_text ""
  dict for {pin value} $missing {
    append insert_text ",\n${indent}.${pin}(${value})"
  }
  return "[string range $stmt 0 [expr {$pos - 1}]]$insert_text[string range $stmt $pos end]"
}

proc ::pin3d::patch_verilog_const_pins {path const_bindings} {
  if {[dict size $const_bindings] == 0} {
    return 0
  }
  set fh [open $path r]
  set text [read $fh]
  close $fh

  set out ""
  set last 0
  set patched 0
  foreach span [::pin3d::split_verilog_statements $text] {
    lassign $span start end
    if {$start > $last} {
      append out [string range $text $last [expr {$start - 1}]]
    }
    set stmt [string range $text $start [expr {$end - 1}]]
    set last $end

    if {[regexp -expanded -lineanchor {(?xs)
        ^\s*
        \S+
        \s+
        (?:\#\s*\([^;]*\)\s+)?
        ((?:\\\S+)|(?:[A-Za-z_][A-Za-z0-9_$]*))
        \s*\(
      } $stmt -> inst_tok]} {
      set inst_norm [::pin3d::normalize_name $inst_tok]
      if {[dict exists $const_bindings $inst_norm]} {
        set stmt2 [::pin3d::append_const_ports_instance $stmt [dict get $const_bindings $inst_norm]]
        if {$stmt2 ne $stmt} {
          incr patched
        }
        append out $stmt2
        continue
      }
    }
    append out $stmt
  }
  if {$last < [string length $text]} {
    append out [string range $text $last end]
  }

  set fh [open $path w]
  puts -nonewline $fh $out
  close $fh
  return $patched
}

# When Yosys preserves hierarchy, OpenROAD can reopen the placed seed more
# reliably from DEF than from the escaped instance names in Verilog.
proc ::pin3d::netlist_has_hier_instance_names {path} {
  if {![file exists $path]} {
    return 0
  }
  set fh [open $path r]
  set found 0
  while {[gets $fh line] >= 0} {
    if {[regexp {^\s*\S+\s+\\\S*/\S*\s*\(} $line]} {
      set found 1
      break
    }
  }
  close $fh
  return $found
}

proc ::pin3d::resolve_run_inputs {} {
  foreach {env_name err_code} {RESULTS_DIR 113 PLATFORM_DIR 114 DESIGN_NAME 115} {
    if {![info exists ::env($env_name)] || $::env($env_name) eq ""} {
      utl::error PIN3D $err_code "$env_name is not set."
    }
  }

  set external_macros [env_var_exists_and_non_empty MACRO_PARTITION_FILE]
  set partition_path [file join $::env(RESULTS_DIR) partition.txt]
  if {$external_macros} {
    set partition_path [file normalize $::env(MACRO_PARTITION_FILE)]
  }

  return [dict create \
    external_macros $external_macros \
    partition_path $partition_path \
    cell_map_path [file join $::env(PLATFORM_DIR) map.json] \
    def_in [file join $::env(RESULTS_DIR) 2_2_floorplan_io.def] \
    v_in [file join $::env(RESULTS_DIR) 2_2_floorplan_io.v] \
    sdc_in [file join $::env(RESULTS_DIR) 1_synth.sdc] \
    v_out [file join $::env(RESULTS_DIR) "${::env(DESIGN_NAME)}_3D.fp.v"] \
    def_out [file join $::env(RESULTS_DIR) "${::env(DESIGN_NAME)}_3D.fp.def"]]
}

proc ::pin3d::load_seed_design {run_inputs} {
  set def_in [dict get $run_inputs def_in]
  set v_in [dict get $run_inputs v_in]
  set sdc_in [dict get $run_inputs sdc_in]

  if {[file exists $def_in] && [::pin3d::netlist_has_hier_instance_names $v_in]} {
    ::pin3d::log "Loading placed 2D DEF snapshot: $def_in (detected hierarchical instance names in $v_in)"
    load_design $def_in $sdc_in "Generate 3D views"
    return
  }

  if {[file exists $def_in]} {
    ::pin3d::log "Using netlist load: $v_in (no hierarchical instance names detected)"
  } else {
    ::pin3d::log "Placed 2D DEF snapshot not found; falling back to netlist load: $v_in"
  }
  load_design $v_in $sdc_in "Generate 3D views"
}

proc ::pin3d::write_outputs {run_inputs const_bindings stats_var} {
  upvar 1 $stats_var stats

  set def_out [dict get $run_inputs def_out]
  set v_out [dict get $run_inputs v_out]
  write_def $def_out
  write_verilog $v_out
  dict set stats extra_pin_injection_count [::pin3d::patch_verilog_const_pins $v_out $const_bindings]
}

# ----------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------

proc ::pin3d::run {} {
  set run_inputs [::pin3d::resolve_run_inputs]
  if {[dict get $run_inputs external_macros]} {
    set part_map [::pin3d::parse_macro_partition_file [dict get $run_inputs partition_path]]
  } else {
    set part_map [::pin3d::parse_partition_file [dict get $run_inputs partition_path]]
  }
  set cell_map [::pin3d::parse_cell_map [dict get $run_inputs cell_map_path]]
  ::pin3d::configure_mixed_library_view
  ::pin3d::load_seed_design $run_inputs

  set db [ord::get_db]
  set block [ord::get_db_block]
  if {$db eq "NULL" || $block eq "NULL"} {
    utl::error PIN3D 116 "OpenROAD database is not initialized."
  }

  set block_context [::pin3d::collect_block_context $block]
  set insts [dict get $block_context insts]
  set norm_to_inst [dict get $block_context norm_to_inst]
  set norm_to_base [dict get $block_context norm_to_base]
  set top_pins [dict get $block_context top_pins]

  if {[dict get $run_inputs external_macros]} {
    set std_tier [_get STD_CELL_TIER bottom]
    set adjusted_part_map [::pin3d::complete_macro_partition $part_map $insts $norm_to_inst $std_tier]
  } else {
    set adjusted_part_map [::pin3d::choose_partition_orientation $part_map $cell_map $norm_to_base $top_pins]
  }
  set replace_result [::pin3d::replace_partitioned_instances $block $db $insts $adjusted_part_map $cell_map $norm_to_inst]
  set stats [dict get $replace_result stats]
  set const_bindings [dict get $replace_result const_bindings]
  ::pin3d::write_outputs $run_inputs $const_bindings stats

  ::pin3d::log [format "Summary: total_instances=%d replaced=%d upper=%d bottom=%d fallback=%d extra_pin_injections=%d ignored_partition_names=%d" \
    [dict get $stats total_instances] \
    [dict get $stats replaced_instances] \
    [dict get $stats upper_count] \
    [dict get $stats bottom_count] \
    [dict get $stats fallback_count] \
    [dict get $stats extra_pin_injection_count] \
    [dict get $stats ignored_partition_names]]
}

if {![info exists ::env(PIN3D_NO_AUTORUN)] || $::env(PIN3D_NO_AUTORUN) eq "" || $::env(PIN3D_NO_AUTORUN) eq "0"} {
  ::pin3d::run
  exit
}
