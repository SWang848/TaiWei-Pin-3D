#!/usr/bin/env python3
"""Collect final OpenROAD partition metrics into one CSV (standard library only)."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import re
import sys


PPA_COLUMNS = ("after_routing_wirelength", "tns", "wns", "power")
COLUMNS = ("idx", *PPA_COLUMNS, "cost")
METRIC_KEYS = {
    "tns": "finish__timing__setup__tns",
    "wns": "finish__timing__setup__ws",
    "power": "finish__power__total",
}
SUMMARY_LABELS = {
    "after_routing_wirelength": "Wire Length",
    "tns": "TNS",
    "wns": "WNS",
    "power": "Total Power",
}
REPORT_NAMES = {
    "openroad_eval.json", "final_summary.txt", "6_wire_length.rpt", "6_finish.rpt",
}
NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def scalar(value: object) -> float | None:
    """Keep missing, invalid, and non-finite measurements out of the CSV."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def read_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        warn(f"cannot read {path}: {error}")
        return ""


def discover_partitions(input_dir: Path) -> list[Path]:
    """Accept a CC run root, its results directory, or one sNNNN directory."""
    if re.fullmatch(r"s\d+", input_dir.name):
        return [input_dir]
    results = input_dir / "results"
    search_root = results if results.is_dir() else input_dir
    partitions = [
        path for path in search_root.iterdir()
        if path.is_dir() and re.fullmatch(r"s\d+", path.name)
    ]
    if not partitions:
        raise ValueError(f"no partition folders named sNNNN found in {search_root}")
    partitions.sort(key=lambda path: int(path.name[1:]))
    indices = [int(path.name[1:]) for path in partitions]
    if len(indices) != len(set(indices)):
        raise ValueError("partition folders have duplicate numeric indices")
    return partitions


def find_reports(partition: Path) -> dict[str, Path | None]:
    """Search unpacked outputs; never follow stale absolute paths inside JSON."""
    candidates: dict[str, list[Path]] = {name: [] for name in REPORT_NAMES}
    for path in partition.rglob("*"):
        if path.name in candidates and path.is_file():
            candidates[path.name].append(path)
    reports: dict[str, Path | None] = {}
    for name, paths in candidates.items():
        # CC saves a convenient copy of the evaluation JSON at the partition root.
        root_copy = partition / name
        if name == "openroad_eval.json" and root_copy in paths:
            reports[name] = root_copy
        elif len(paths) > 1:
            raise ValueError(
                f"{partition}: multiple {name} files; select a single run per partition"
            )
        else:
            reports[name] = paths[0] if paths else None
    return reports


def routed_wirelength(text: str) -> float | None:
    """Sum final per-net DRT lengths, matching the flow's final-summary convention."""
    values = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0] == "drt:":
            value = scalar(fields[2])
            if value is None:
                return None
            values.append(value)
    # The Tcl summary adds each net in report order before formatting to 2 decimals.
    return round(sum(values), 2) if values else None


def collect_partition(partition: Path) -> dict[str, int | float | None]:
    reports = find_reports(partition)
    row: dict[str, int | float | None] = dict.fromkeys(COLUMNS)
    row["idx"] = int(partition.name[1:])

    text = read_text(reports["openroad_eval.json"])
    if text:
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            for column, key in METRIC_KEYS.items():
                metric = payload.get(key)
                row[column] = scalar(metric.get("value") if isinstance(metric, dict) else metric)
        except ValueError as error:
            warn(f"{partition.name}: invalid evaluation JSON: {error}")

    summary = read_text(reports["final_summary.txt"])
    for column, label in SUMMARY_LABELS.items():
        if row[column] is None:
            match = re.search(rf"^{re.escape(label)}[ \t]+(\S+)[ \t]*$", summary, re.M)
            if match:
                row[column] = scalar(match.group(1))

    if row["after_routing_wirelength"] is None:
        row["after_routing_wirelength"] = routed_wirelength(
            read_text(reports["6_wire_length.rpt"])
        )

    if any(row[column] is None for column in METRIC_KEYS):
        finish = read_text(reports["6_finish.rpt"])
        patterns = {
            "tns": rf"^tns max[ \t]+({NUMBER})(?=\s|$)",
            "wns": rf"^wns max[ \t]+({NUMBER})(?=\s|$)",
            "power": rf"^Total[ \t]+{NUMBER}[ \t]+{NUMBER}[ \t]+{NUMBER}[ \t]+({NUMBER})(?=\s|$)",
        }
        for column, pattern in patterns.items():
            if row[column] is None:
                match = re.search(pattern, finish, re.M)
                if match:
                    row[column] = scalar(match.group(1))

    missing = [column for column in PPA_COLUMNS if row[column] is None]
    if missing:
        warn(f"{partition.name}: missing final metrics: {', '.join(missing)}; leaving blanks")
    return row


def add_costs(rows: list[dict[str, int | float | None]]) -> None:
    """Compute distance from the best normalized PPA, using complete rows only."""
    complete = []
    for row in rows:
        row["cost"] = None
        if all(row[column] is not None for column in PPA_COLUMNS):
            complete.append(row)
    if not complete:
        return

    bounds = {}
    for column in PPA_COLUMNS:
        values = [value for row in complete if (value := row[column]) is not None]
        bounds[column] = (min(values), max(values))

    for row in complete:
        normalized = []
        for column in PPA_COLUMNS:
            value = row[column]
            assert value is not None  # Complete rows have all four measurements.
            minimum, maximum = bounds[column]
            if maximum == minimum:
                component = 0.0
            elif column in ("tns", "wns"):
                # Higher (less negative) slack is better.
                component = (maximum - value) / (maximum - minimum)
            else:
                component = (value - minimum) / (maximum - minimum)
            normalized.append(component)
        row["cost"] = math.hypot(*normalized)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize final PPA for unpacked OpenROAD CC partition results.",
        epilog=(
            "Columns: idx (s0048 -> 48), after_routing_wirelength (um), tns (ns), "
            "wns (ns), power (W), cost (L2 norm of min-max normalized PPA; lower is better). "
            "Normalization uses complete rows only; constant metrics contribute zero. "
            "Missing final metrics and their cost are blank, never zero. "
            "Extract output.tar archives before running this program."
        ),
    )
    parser.add_argument("input_dir", type=Path, help="CC run root, results directory, or sNNNN folder")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Output CSV path (default: INPUT_DIR/partition_ppa.csv)",
    )
    args = parser.parse_args(argv)
    input_dir = args.input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        parser.error(f"input directory does not exist: {input_dir}")
    output = args.output.expanduser() if args.output else input_dir / "partition_ppa.csv"
    if output.suffix.lower() != ".csv":
        parser.error("output filename must end in .csv")
    try:
        partitions = discover_partitions(input_dir)
        rows = [collect_partition(partition) for partition in partitions]
        add_costs(rows)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    complete = sum(all(row[column] is not None for column in PPA_COLUMNS) for row in rows)
    print(f"Wrote {len(rows)} partitions to {output.resolve()} ({complete} complete, {len(rows) - complete} incomplete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
