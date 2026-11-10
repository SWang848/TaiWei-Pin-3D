# Minimal OpenDB API fixture. Executed by tclsh as a stand-in for OpenROAD so
# the real export_sa_graph.tcl adapter is exercised without an EDA install.
proc read_db {path} {
  if {![file isfile $path]} {error "Seed not found: $path"}
}
namespace eval ord {
  proc get_db_block {} {return block}
  proc get_db_tech {} {return tech}
}
proc tech {method} {
  if {$method eq "getDbUnitsPerMicron"} {return 1000}
  error "Unexpected technology method: $method"
}
proc block {method} {
  switch -- $method {
    getHierarchyDelimiter {return /}
    getInsts {return {a b c logic}}
    getBTerms {return {p0 p1 p2 p3 p4 p5 p6 unconnected_port}}
    getNets {return {parallel bc boundary10 boundary9 clock power empty}}
  }
  error "Unexpected block method: $method"
}
proc instance {id method} {
  switch -- $method {
    getName {return [dict get {a top/group/a b top/group/b c top/other/c logic logic0} $id]}
    getMaster {return [dict get {a ram_a b ram_b c ram_c logic core} $id]}
  }
  error "Unexpected instance method: $method"
}
proc master {id method} {
  switch -- $method {
    getName {return $id}
    getType {return [expr {$id eq "core" ? "CORE" : "BLOCK"}]}
    getWidth {return [dict get {ram_a 10000 ram_b 5000 ram_c 10000 core 2000} $id]}
    getHeight {return [dict get {ram_a 20000 ram_b 10000 ram_c 10000 core 3000} $id]}
  }
  error "Unexpected master method: $method"
}
proc term {id method} {
  if {$method eq "getInst"} {return $id}
  error "Unexpected term method: $method"
}
proc net {id method} {
  switch -- $method {
    getSigType {return [expr {$id eq "power" ? "POWER" : ($id eq "clock" ? "CLOCK" : "SIGNAL")}]}
    getITerms {
      return [dict get {
        parallel {pa pa pb pl} bc {pb pc} boundary10 {pa pb pl}
        boundary9 {pa pc} clock {pb pl} power {pa pc pl} empty {}
      } $id]
    }
    getBTerms {
      if {$id in {boundary10 boundary9}} {return {p0 p1 p2 p3 p4 p5 p6}}
      return {}
    }
  }
  error "Unexpected net method: $method"
}
foreach id {a b c logic} {interp alias {} $id {} instance $id}
foreach id {ram_a ram_b ram_c core} {interp alias {} $id {} master $id}
foreach {pin inst} {pa a pb b pc c pl logic} {interp alias {} $pin {} term $inst}
foreach id {parallel bc boundary10 boundary9 clock power empty} {interp alias {} $id {} net $id}
source [lindex $argv end]
