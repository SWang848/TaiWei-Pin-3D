#!/usr/bin/env python3
"""Generate per-solution macro partition inputs from a Pareto candidate JSON.

Example, from the repository root:
    python3 generate_macro_partitions.py \
        partition_experiments/nangate45_3D/bp_be/candidates.json

Outputs go beside the input JSON. For a registered case, prepare a private 2D
seed and read actual macro names from OpenROAD before converting candidates.
No previous run, name map, reference partition or TritonPart result is needed.
Candidate solution[0] is bottom and solution[1] is upper. Exported Taiwei3D
labels are 1 for bottom and 0 for upper. No 3D candidate jobs are launched.
The 2D seed is retained under preparation/results beside the solutions so the
output is ready for CC evaluation. Use --discard-preparation for partition-only
export when the retained seed is not needed.
"""

import argparse
from contextlib import contextmanager, nullcontext
import csv
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


REPO_ROOT = Path(__file__).resolve().parent
PREPARATION_CONTENTS = {
    ".prepare.lock", "macro_names.tsv", "seed_info.json", "prepare.log",
    "results", "logs", "objects", "reports",
}
CC_SEED_FILES = ("1_synth.sdc", "2_2_floorplan_io.def", "2_2_floorplan_io.v")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def read_json(path):
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def normalize_name(name):
    name = name.strip()
    if name.startswith("\\"):
        name = name[1:]
    return re.sub(r"\\([/\[\]])", r"\1", name)


def folded_name(name):
    # Compare aliases against known instances; never replace '_' with '/'.
    return normalize_name(name).replace("/", "_").replace(".", "_")


def read_name_map(path):
    aliases = {}
    masters = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not {"external_name", "instance_name"}.issubset(reader.fieldnames or []):
            raise ValueError(f"{path}: expected external_name and instance_name columns")
        for row in reader:
            external = row.get("external_name")
            actual = row.get("instance_name")
            if not external or not actual:
                raise ValueError(f"{path}:{reader.line_num}: missing macro name")
            actual = normalize_name(actual)
            if not actual or re.search(r"\s", actual):
                raise ValueError(f"{path}:{reader.line_num}: invalid instance name")
            if external in aliases:
                raise ValueError(f"{path}:{reader.line_num}: duplicate alias {external!r}")
            aliases[external] = actual
            master = row.get("master_name") or ""
            if actual in masters and masters[actual] != master:
                raise ValueError(f"{path}: conflicting masters for {actual!r}")
            masters[actual] = master
    if not aliases:
        raise ValueError(f"Macro name map is empty: {path}")
    return aliases, masters


def read_reference_partition(path):
    masters = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        fields = line.split()
        if len(fields) != 2 or fields[1] not in ("0", "1"):
            raise ValueError(f"{path}:{line_no}: invalid reference partition row")
        name = normalize_name(fields[0])
        if not name or re.search(r"\s", name):
            raise ValueError(f"{path}:{line_no}: invalid reference instance name")
        if name in masters:
            raise ValueError(f"{path}:{line_no}: duplicate reference macro {name!r}")
        masters[name] = ""
    if not masters:
        raise ValueError(f"Reference partition is empty: {path}")
    return {}, masters


def read_macro_inventory(path):
    """Read the complete macro catalog exported from a loaded OpenDB design."""
    masters = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not {"instance_name", "master_name"}.issubset(reader.fieldnames or []):
            raise ValueError(f"{path}: expected instance_name and master_name columns")
        for row in reader:
            name = normalize_name(row.get("instance_name") or "")
            master = row.get("master_name")
            if not name or re.search(r"\s", name) or not master:
                raise ValueError(f"{path}:{reader.line_num}: invalid macro inventory row")
            if name in masters:
                raise ValueError(f"{path}:{reader.line_num}: duplicate loaded macro {name!r}")
            masters[name] = master
    if not masters:
        raise ValueError(f"No BLOCK macros found in the loaded 2D seed: {path}")
    return masters


def resolve_design_config(candidates, design_config=None):
    if design_config is not None:
        config = Path(design_config).resolve()
        if not config.is_file():
            raise ValueError(f"2D design config not found: {config}")
        return config
    try:
        relative = candidates.parent.relative_to(REPO_ROOT / "partition_experiments")
    except ValueError:
        return None
    if len(relative.parts) >= 2:
        config = REPO_ROOT / "designs" / relative.parts[0] / relative.parts[1] / "config2d.mk"
        if config.is_file():
            return config
    return None


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_macro_inventory(config, preparation_dir, rebuild_seed=False):
    """Bootstrap only the 2D front end, isolated from normal result variants."""
    preparation_dir.mkdir(parents=True, exist_ok=True)
    inventory = preparation_dir / "macro_names.tsv"
    state_path = preparation_dir / "seed_info.json"
    identity = {
        "design_config": str(config),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
    }
    required = [preparation_dir / "results" / name for name in (
        "1_synth.sdc", "2_2_floorplan_io.odb", "2_2_floorplan_io.def", "2_2_floorplan_io.v",
    )]
    # Multiple generators may share one case's preparation directory.
    with (preparation_dir / ".prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = read_json(state_path) if state_path.is_file() else {}
        except (OSError, ValueError):
            state = {}
        if (not rebuild_seed and isinstance(state, dict)
                and state.get("status") == "complete"
                and all(state.get(key) == value for key, value in identity.items())
                and inventory.is_file() and all(path.is_file() for path in required)
                and state.get("inventory_sha256") == file_sha256(inventory)
                and state.get("seed_sha256") == {path.name: file_sha256(path) for path in required}):
            print(f"[prepare] Reusing 2D seed: {preparation_dir}", flush=True)
            return read_macro_inventory(inventory)

        state = {**identity, "status": "preparing"}
        write_if_changed(state_path, json.dumps(state, indent=2) + "\n")
        command = [
            "make", "--no-print-directory", "-B", "-j1",
            f"DESIGN_CONFIG={config}", f"FLOW_HOME={REPO_ROOT}",
            f"DESIGN_HOME={REPO_ROOT / 'designs'}", f"PLATFORM_HOME={REPO_ROOT / 'platforms'}",
            f"OPENROAD_SCRIPTS_DIR={REPO_ROOT / 'scripts_openroad'}", f"UTILS_DIR={REPO_ROOT / 'util'}",
            f"WORK_HOME={preparation_dir}", "FLOW_VARIANT=macro_seed", "USE_FLOW=openroad",
            "DESIGN_DIMENSION=2D", "MACRO_PARTITION_FILE=",
            f"RESULTS_DIR={preparation_dir / 'results'}", f"LOG_DIR={preparation_dir / 'logs'}",
            f"OBJECTS_DIR={preparation_dir / 'objects'}", f"REPORTS_DIR={preparation_dir / 'reports'}",
            f"MACRO_NAMES_FILE={inventory}",
            "ord-check-macro-tools", "ord-synth", "ord-preplace", "ord-export-macro-names",
        ]
        log_path = preparation_dir / "prepare.log"
        print(f"[prepare] Building 2D seed and exporting OpenDB macro names; log: {log_path}", flush=True)
        with log_path.open("w", encoding="utf-8") as log:
            log.write("Command: " + " ".join(command) + "\n")
            log.flush()
            result = subprocess.run(command, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
            raise RuntimeError(
                f"2D preparation failed (exit {result.returncode}). Check the Yosys/OpenROAD toolchain. "
                f"Log: {log_path}\n{tail}"
            )
        missing = [str(path) for path in [*required, inventory] if not path.is_file()]
        if missing:
            raise RuntimeError(f"2D preparation did not produce required files: {missing}. Log: {log_path}")
        masters = read_macro_inventory(inventory)
        state.update(
            status="complete", macro_count=len(masters),
            inventory_sha256=file_sha256(inventory),
            seed_sha256={path.name: file_sha256(path) for path in required},
        )
        write_if_changed(state_path, json.dumps(state, indent=2) + "\n")
        return masters


def load_macro_names(candidates, name_map=None, reference_partition=None,
                     design_config=None, preparation_dir=None, rebuild_seed=False):
    if reference_partition is not None:
        return read_reference_partition(Path(reference_partition))
    if name_map is not None:
        return read_name_map(Path(name_map))
    config = resolve_design_config(candidates, design_config)
    adjacent_map = candidates.parent / "macro_name_map.tsv"
    if config is not None:
        preparation_dir = Path(preparation_dir) if preparation_dir is not None else candidates.parent / "preparation"
        masters = prepare_macro_inventory(config, preparation_dir, rebuild_seed)
        # Old aliases can help disambiguate names, but their targets must still
        # exist in the newly loaded seed. The seed defines the macro catalog.
        aliases = read_name_map(adjacent_map)[0] if adjacent_map.is_file() else {}
        aliases = {external: actual for external, actual in aliases.items() if actual in masters}
        return aliases, masters
    if adjacent_map.is_file():
        return read_name_map(adjacent_map)
    raise ValueError(
        "Cannot locate the case's config2d.mk. Use --design-config for a new design, "
        "or --name-map/--reference-partition for an offline conversion."
    )


def tsv_text(rows):
    handle = io.StringIO(newline="")
    csv.writer(handle, delimiter="\t", lineterminator="\n").writerows(rows)
    return handle.getvalue()


def read_candidate_records(candidates):
    data = read_json(candidates)
    archive = data.get("pareto_archive") if isinstance(data, dict) else None
    records = archive.get("solutions") if isinstance(archive, dict) else None
    if not isinstance(records, dict) or not records:
        raise ValueError("Expected a nonempty pareto_archive.solutions object")
    for key, record in records.items():
        groups = record.get("solution") if isinstance(record, dict) else None
        cost = record.get("cost") if isinstance(record, dict) else None
        if not isinstance(groups, list) or len(groups) != 2 or not all(isinstance(group, list) for group in groups):
            raise ValueError(f"{key}: solution must contain exactly two lists")
        if not isinstance(cost, list) or len(cost) != 2 or not all(
            type(value) in (int, float) and math.isfinite(value) for value in cost
        ):
            raise ValueError(f"{key}: cost must contain exactly two finite numbers")
        if not all(isinstance(name, str) for group in groups for name in group):
            raise ValueError(f"{key}: macro names must be strings")
    return records


def prepare_files(candidates, aliases, masters):
    records = read_candidate_records(candidates)

    expected_names = set(masters)
    folded = {}
    for name in expected_names:
        folded.setdefault(folded_name(name), []).append(name)
    files = {}
    used_aliases = {}
    signatures = {}
    manifest = [[
        "solution_id", "source_index", "source_key", "cost_0", "cost_1",
        "upper_macros", "bottom_macros", "duplicate_of", "partition_file", "metadata_file",
    ]]
    for index, (key, record) in enumerate(records.items()):
        solution_id = f"s{index:04d}"
        groups = record.get("solution") if isinstance(record, dict) else None
        cost = record.get("cost") if isinstance(record, dict) else None
        assignments = {}
        for group_index, group in enumerate(groups):
            # Candidate array indices and Taiwei3D physical labels are opposite.
            tier = 1 - group_index
            for external in group:
                name = normalize_name(external)
                if name in expected_names:
                    actual = name
                elif external in aliases:
                    actual = aliases[external]
                else:
                    matches = folded.get(folded_name(external), [])
                    if len(matches) != 1:
                        raise ValueError(f"{key}: unknown or ambiguous macro {external!r}: {matches}")
                    actual = matches[0]
                if actual in assignments:
                    raise ValueError(f"{key}: macro assigned more than once: {actual}")
                assignments[actual] = tier
                used_aliases[external] = actual
        missing = expected_names - assignments.keys()
        if missing:
            raise ValueError(f"{key}: missing macros: {', '.join(sorted(missing))}")

        partition_path = f"solutions/{solution_id}/macro_partition.txt"
        metadata_path = f"solutions/{solution_id}/solution.json"
        source_label = key.replace("\r", "\\r").replace("\n", "\\n")
        lines = [
            f"# Source: {candidates.name} / pareto_archive.solutions / {source_label}",
            f"# Solution ID: {solution_id}",
            "# Candidate groups: solution[0] = bottom; solution[1] = upper.",
            "# Physical tier labels: 0 = upper, 1 = bottom.",
            "# Standard-cell tier is selected separately using STD_CELL_TIER.",
        ]
        lines.extend(f"{name} {tier}" for name, tier in assignments.items())
        files[partition_path] = "\n".join(lines) + "\n"
        metadata = {
            "solution_id": solution_id,
            "source_index": index,
            "source_key": key,
            "cost": cost,
        }
        files[metadata_path] = json.dumps(metadata, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        signature = tuple(sorted(assignments.items()))
        duplicate_of = signatures.setdefault(signature, solution_id)
        manifest.append([
            solution_id, index, key, *cost, len(groups[1]), len(groups[0]),
            "" if duplicate_of == solution_id else duplicate_of, partition_path, metadata_path,
        ])

    files["manifest.tsv"] = tsv_text(manifest)
    files["macro_name_map.tsv"] = tsv_text([
        ["external_name", "instance_name", "master_name"],
        *[[name, actual, masters[actual]] for name, actual in sorted(used_aliases.items())],
    ])
    return files, len(records)


def check_destination(output_dir, files):
    expected_ids = {Path(name).parts[1] for name in files if name.startswith("solutions/")}
    solutions_dir = output_dir / "solutions"
    if solutions_dir.is_dir():
        for entry in solutions_dir.iterdir():
            if entry.is_dir() and re.fullmatch(r"s\d+", entry.name) and entry.name not in expected_ids:
                raise ValueError(f"Stale solution folder {entry}; use a fresh --output-dir")
    for relative, text in files.items():
        if not relative.endswith("/solution.json"):
            continue
        path = output_dir / relative
        if path.is_file():
            old = read_json(path)
            new = json.loads(text)
            if not isinstance(old, dict) or any(old.get(key) != new[key] for key in ("source_index", "source_key")):
                raise ValueError(f"Solution identity differs at {path}; use a fresh --output-dir")


def write_if_changed(path, text):
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@contextmanager
def preparation_lifecycle_lock(preparation_dir):
    # Keep this lock outside the removable directory so a waiting generator
    # cannot acquire a lock on an inode that cleanup has already unlinked.
    preparation_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = preparation_dir.parent / f".{preparation_dir.name}.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def check_preparation_cleanup_path(preparation_dir, candidates, output_dir, config):
    protected = (candidates, output_dir, REPO_ROOT, config)
    if any(path == preparation_dir or preparation_dir in path.parents for path in protected):
        raise ValueError("Preparation directory must not contain the input, output or repository/config files")
    if preparation_dir.exists():
        if not preparation_dir.is_dir():
            raise ValueError(f"Preparation path is not a directory: {preparation_dir}")
        unknown = {path.name for path in preparation_dir.iterdir()} - PREPARATION_CONTENTS
        if unknown:
            raise ValueError(
                f"Preparation directory contains unrelated files: {sorted(unknown)}; "
                "choose a dedicated --preparation-dir or use --keep-preparation"
            )


def export_cc_seed(preparation_dir, output_dir):
    """Publish the CC handoff even when the reusable cache is elsewhere."""
    destination = output_dir / "preparation" / "results"
    for name in CC_SEED_FILES:
        source = preparation_dir / "results" / name
        target = destination / name
        if source.resolve() == target.resolve():
            continue
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()


def generate(candidates, output_dir=None, name_map=None, reference_partition=None, *,
             design_config=None, preparation_dir=None, rebuild_seed=False, keep_preparation=True):
    candidates = Path(candidates).resolve()
    output_dir = Path(output_dir).resolve() if output_dir is not None else candidates.parent
    if (name_map is not None or reference_partition is not None) and (design_config is not None or rebuild_seed):
        raise ValueError("Use either 2D preparation options or an explicit offline name reference, not both")
    read_candidate_records(candidates)  # Reject malformed input before starting EDA tools.
    preparation_dir = Path(preparation_dir).resolve() if preparation_dir is not None else output_dir / "preparation"
    config = None
    if name_map is None and reference_partition is None:
        config = resolve_design_config(candidates, design_config)
    if config is not None and not keep_preparation:
        check_preparation_cleanup_path(preparation_dir, candidates, output_dir, config)
    lock = preparation_lifecycle_lock(preparation_dir) if config is not None else nullcontext()
    with lock:
        if config is not None and not keep_preparation:
            check_preparation_cleanup_path(preparation_dir, candidates, output_dir, config)
        aliases, masters = load_macro_names(
            candidates, name_map, reference_partition, design_config, preparation_dir, rebuild_seed,
        )
        files, count = prepare_files(candidates, aliases, masters)
        # Validate the entire archive and all existing identities before writing.
        check_destination(output_dir, files)
        if config is not None and keep_preparation:
            export_cc_seed(preparation_dir, output_dir)
        for relative, text in files.items():
            write_if_changed(output_dir / relative, text)
        # Only a successful preparation and complete export reach cleanup.
        # Offline conversions never delete a preparation directory they did not use.
        if config is not None and not keep_preparation:
            check_preparation_cleanup_path(preparation_dir, candidates, output_dir, config)
            shutil.rmtree(preparation_dir)
            print(f"[prepare] Removed completed preparation directory: {preparation_dir}", flush=True)
    return count, output_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidates", type=Path, help="Candidate JSON containing pareto_archive.solutions")
    parser.add_argument("--output-dir", type=Path, help="Output directory (default: input JSON directory)")
    parser.add_argument("--design-config", type=Path, help="2D design config; inferred from partition_experiments/<platform>/<case>/ by default")
    parser.add_argument("--preparation-dir", type=Path, help="Private 2D preparation directory (default: <output-dir>/preparation)")
    parser.add_argument("--rebuild-seed", action="store_true", help="Rebuild cached preparation after RTL or library changes")
    preparation = parser.add_mutually_exclusive_group()
    preparation.add_argument("--keep-preparation", action="store_true", help="Retain the 2D seed and logs for CC evaluation and reuse (default)")
    preparation.add_argument("--discard-preparation", dest="keep_preparation", action="store_false", help="Remove the 2D cache after export when only partition files are needed")
    parser.set_defaults(keep_preparation=True)
    names = parser.add_mutually_exclusive_group()
    names.add_argument("--name-map", type=Path, help="Verified TSV macro name map")
    names.add_argument("--reference-partition", type=Path, help="Existing partition file containing every macro's exact name")
    args = parser.parse_args()
    try:
        count, output_dir = generate(
            args.candidates, args.output_dir, args.name_map, args.reference_partition,
            design_config=args.design_config, preparation_dir=args.preparation_dir,
            rebuild_seed=args.rebuild_seed, keep_preparation=args.keep_preparation,
        )
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")
    print(f"Generated {count} solution folders under {output_dir / 'solutions'}")
    seed = output_dir / "preparation" / "results"
    if all((seed / name).is_file() for name in CC_SEED_FILES):
        print(f"2D seed inputs for CC evaluation: {seed}")
    else:
        print("Partition-only export: a matching 2D seed is still required for CC evaluation.")
    print("Metadata fields: solution_id, source_index, source_key, cost")
    print("Candidate groups: solution[0] -> bottom (label 1); solution[1] -> upper (label 0).")
    print("Physical tier labels: 0 = upper; 1 = bottom. No 3D candidate jobs launched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
