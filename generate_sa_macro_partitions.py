#!/usr/bin/env python3
"""Run grid-based macro-tier SA directly from a TaiWei-Pin-3D 2D design config.

    python3 generate_sa_macro_partitions.py designs/nangate45_3D/bp_be/config2d.mk

Builds a private 2D seed, reads its OpenDB instances and nets, and writes the same
solutions/, manifest.tsv, macro_name_map.tsv and preparation/ handoff as
generate_macro_partitions.py, plus candidates.json and hierarchy_tree.txt.
Use --seed-results to import an existing matching 2D seed without synthesis.
Reruns reuse preparation and replace generated partitions with the new settings.
Matplotlib saves a cut/imbalance scatter plot as PNG and PDF after each run.
Use --plot-only CANDIDATES to plot an existing archive without rerunning SA,
or --no-plot to generate partitions using only Python's standard library.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from itertools import combinations
import csv
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import tempfile

import generate_macro_partitions as exporter


# Internal array indices follow old_SA.py. Only the exporter converts these to
# TaiWei physical labels (1 = bottom, 0 = upper).
BOTTOM_DIE = 0
UPPER_DIE = 1
REPO_ROOT = Path(__file__).resolve().parent
SEED_FILES = (*exporter.CC_SEED_FILES, "2_2_floorplan_io.odb")
LOG = logging.getLogger(__name__)
PLOT_FILES = ("partition_solutions.png", "partition_solutions.pdf")


@dataclass(frozen=True)
class Node:
    name: str
    master: str
    is_macro: bool
    area: float  # square microns, including standard cells


@dataclass
class Graph:
    nodes: dict = field(default_factory=dict)
    adjacency: dict = field(default_factory=dict)
    hierarchy_separator: str = "/"

    @cached_property
    def macro_ids(self):
        return [node_id for node_id, node in self.nodes.items() if node.is_macro]

    @cached_property
    def standard_cell_area(self):
        return math.fsum(node.area for node in self.nodes.values() if not node.is_macro)

    @cached_property
    def total_area(self):
        return math.fsum(node.area for node in self.nodes.values())

    def metrics(self, solution):
        """Full recomputation, independent of the incremental move bookkeeping."""
        if len(solution) != 2:
            raise ValueError("Solution must contain exactly two macro groups")
        upper = set(solution[UPPER_DIE])
        bottom = set(solution[BOTTOM_DIE])
        if upper & bottom or upper | bottom != set(self.macro_ids):
            raise ValueError("Solution must assign every macro exactly once")
        if sum(map(len, solution)) != len(upper | bottom):
            raise ValueError("Duplicate macro in solution")
        # Every crossing edge has exactly one endpoint in upper. Fixed
        # standard-cell edges cannot cross, so they need not be scanned again
        # for each archive entry on large designs.
        cut = sum(weight for u in upper for v, weight in self.adjacency[u].items() if v not in upper)
        areas = [math.fsum([self.standard_cell_area, *(self.nodes[i].area for i in bottom)]),
                 math.fsum(self.nodes[i].area for i in upper)]
        return cut, abs(areas[UPPER_DIE] - areas[BOTTOM_DIE]), areas


def graph_construction(path, net_pin_limit=10):
    """Expand each signal net with <net_pin_limit pins into a weighted clique.

    Adjacency counters represent parallel MultiGraph edges, including repeated
    instance pins. Self-loops can never cross tiers and are omitted. IO pins
    count toward the fanout filter but do not become vertices.
    """
    if net_pin_limit < 2:
        raise ValueError("net_pin_limit must be at least 2")
    graph = Graph()
    names = set()
    with Path(path).open(encoding="utf-8", newline="") as handle:
        if handle.readline().rstrip("\r\n") != "# taiwei-sa-graph-v1":
            raise ValueError(f"{path}: unsupported SA graph format")
        rows = csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)
        hierarchy_seen = False
        nets_seen = False
        for line_no, row in enumerate(rows, 2):
            try:
                if len(row) == 2 and row[0] == "hierarchy" and not hierarchy_seen and not graph.nodes:
                    if not row[1] or any(c.isspace() for c in row[1]):
                        raise ValueError("invalid hierarchy delimiter")
                    graph.hierarchy_separator = row[1]
                    hierarchy_seen = True
                elif len(row) == 6 and row[0] == "node" and not nets_seen:
                    _, raw_id, raw_name, master, macro, raw_area = row
                    node_id, area = int(raw_id), float(raw_area)
                    name = exporter.normalize_name(raw_name)
                    if (node_id < 0 or node_id in graph.nodes or name in names or not name
                            or any(c.isspace() for c in name) or not master
                            or macro not in ("0", "1") or not math.isfinite(area) or area <= 0):
                        raise ValueError("invalid or duplicate instance")
                    names.add(name)
                    graph.nodes[node_id] = Node(name, master, macro == "1", area)
                    graph.adjacency[node_id] = Counter()
                elif len(row) >= 2 and row[0] == "net":
                    nets_seen = True
                    pin_count = int(row[1])
                    pins = [int(value) for value in row[2:]]
                    if pin_count < len(pins) or any(i not in graph.nodes for i in pins):
                        raise ValueError("invalid net pins")
                    if pin_count < net_pin_limit:
                        for u, v in combinations(pins, 2):
                            if u != v:
                                graph.adjacency[u][v] += 1
                                graph.adjacency[v][u] += 1
                else:
                    raise ValueError("invalid graph row")
            except ValueError as error:
                raise ValueError(f"{path}:{line_no}: {error}") from error
    if not hierarchy_seen or not graph.nodes or not graph.macro_ids:
        raise ValueError(f"{path}: expected a hierarchy delimiter and at least one BLOCK macro")
    return graph


@dataclass(frozen=True)
class HierarchyNode:
    hierarchy_name: str
    macro_ids: tuple


class HierarchyTree:
    """Group exact OpenDB macro names by their hierarchy path prefixes."""

    def __init__(self, graph, separator=None):
        separator = graph.hierarchy_separator if separator is None else separator
        if not separator or any(c.isspace() for c in separator):
            raise ValueError("hierarchy separator must be nonempty and contain no whitespace")
        prefixes = defaultdict(list)
        for node_id in graph.macro_ids:
            parts = graph.nodes[node_id].name.split(separator)
            for depth in range(1, len(parts) + 1):
                prefixes[tuple(parts[:depth])].append(node_id)
        self.nodes_by_level = defaultdict(list)
        self.nodes_by_path = {}
        for parts, ids in prefixes.items():
            node = HierarchyNode(separator.join(parts), tuple(ids))
            self.nodes_by_level[len(parts)].append(node)
            self.nodes_by_path[parts] = node
        self.leaves = [HierarchyNode(graph.nodes[i].name, (i,)) for i in graph.macro_ids]
        self.max_depth = max(self.nodes_by_level)

    def as_text(self):
        rows = ["root"]
        for path, node in sorted(self.nodes_by_path.items()):
            rows.append(f"{'  ' * len(path)}{node.hierarchy_name} [{len(node.macro_ids)} macros]")
        return "\n".join(rows) + "\n"


class HMSA:
    """The reference's hierarchy-aware, log-normalized multi-objective SA.

    Standard cells stay on bottom and contribute to both the cut and area.
    A hierarchy move gathers all its macros onto the opposite tier of its first
    macro. The default grid retains recent accepted states per cell; it does
    not filter dominated states. True Pareto filtering is an optional mode.
    """

    def __init__(self, graph, *, seed=42, grid_size=40, grid_cell_capacity=3,
                 grid_based_multi_objective=True, hierarchy_separator=None):
        if grid_size < 1 or grid_cell_capacity < 1:
            raise ValueError("grid size and cell capacity must be positive")
        self.G = graph
        self.rng = random.Random(seed)
        self.grid_size = grid_size
        self.grid_cell_capacity = grid_cell_capacity
        self.grid_based_multi_objective = grid_based_multi_objective
        self.hierarchy_tree = HierarchyTree(graph, hierarchy_separator)
        self.pareto_archive_grid = {}
        self.norm_bounds = None
        solution = [[], []]
        for node_id in graph.macro_ids:
            solution[UPPER_DIE if self.rng.random() < 0.5 else BOTTOM_DIE].append(node_id)
        self.reset_state_from_solution(solution)

    @staticmethod
    def validate_schedule(T_max, T_min, gamma, steps_per_T):
        if not all(math.isfinite(value) for value in (T_max, T_min, gamma)):
            raise ValueError("Annealing temperatures and gamma must be finite")
        if not T_max > T_min > 0 or not 0 < gamma < 1 or steps_per_T < 1:
            raise ValueError("Require T_max > T_min > 0, 0 < gamma < 1, and positive steps per temperature")

    def reset_state_from_solution(self, solution):
        cut, imbalance, areas = self.G.metrics(solution)
        self.current_solution = [list(group) for group in solution]
        self.partitions = {i: BOTTOM_DIE for i in self.G.nodes}
        self.partitions.update({i: UPPER_DIE for i in solution[UPPER_DIE]})
        self.current_total_cutsize = cut
        self.current_imbalance = imbalance
        self.current_area_balance = areas
        self._update_norm_bounds(cut, imbalance)
        self._update_pareto_archive((cut, imbalance), self.current_solution)

    def _moving_ids(self, node):
        source = self.partitions[node.macro_ids[0]]
        return source, tuple(i for i in node.macro_ids if self.partitions[i] == source)

    def calculate_deltas(self, node_to_move):
        source, moving = self._moving_ids(node_to_move)
        moving_set = set(moving)
        # Only edges with exactly one moved endpoint change their cut status.
        # In particular, mixed-tier hierarchy groups must not count an internal
        # edge a second time from a descendant already on the target tier.
        delta_cut = sum(weight if self.partitions[v] == source else -weight
                        for u in moving for v, weight in self.G.adjacency[u].items()
                        if v not in moving_set)
        delta_area = math.fsum(self.G.nodes[i].area for i in moving)
        return delta_cut, delta_area

    def commit_move(self, node_to_move, delta_cut, delta_area):
        source, moving = self._moving_ids(node_to_move)
        target = 1 - source
        moving_set = set(moving)
        self.current_solution[source] = [i for i in self.current_solution[source] if i not in moving_set]
        self.current_solution[target].extend(moving)
        self.partitions.update({i: target for i in moving})
        self.current_total_cutsize += delta_cut
        self.current_area_balance[source] -= delta_area
        self.current_area_balance[target] += delta_area
        self.current_imbalance = abs(self.current_area_balance[1] - self.current_area_balance[0])

    def _get_normalized_cost(self, cut, imbalance):
        result = []
        for value, bounds in zip((cut, imbalance), self.norm_bounds):
            low, high = bounds
            result.append(0.5 if high == low else (math.log1p(value) - low) / (high - low))
        return tuple(result)

    def _update_norm_bounds(self, cut, imbalance):
        logs = (math.log1p(cut), math.log1p(imbalance))
        if self.norm_bounds is None:
            self.norm_bounds = [(value, value) for value in logs]
            return
        bounds = [(min(low, value), max(high, value))
                  for (low, high), value in zip(self.norm_bounds, logs)]
        changed = bounds != self.norm_bounds
        self.norm_bounds = bounds
        if self.grid_based_multi_objective and changed:
            entries = [entry for cell in self.pareto_archive_grid.values() for entry in cell]
            self.pareto_archive_grid = {}
            for cost, solution in entries:
                self._update_pareto_archive(cost, solution)

    @staticmethod
    def _dominates(lhs, rhs):
        return lhs[0] <= rhs[0] and lhs[1] <= rhs[1] and lhs != rhs

    def _update_pareto_archive(self, cost, solution):
        snapshot = tuple(tuple(group) for group in solution)
        if self.grid_based_multi_objective:
            cell = []
            for value, (low, high) in zip(cost, self.norm_bounds):
                ratio = 0 if low == high else (math.log1p(value) - low) / (high - low + 1e-9)
                cell.append(max(0, min(self.grid_size - 1, int(ratio * self.grid_size))))
            entries = self.pareto_archive_grid.setdefault(tuple(cell), [])
            entries.append((cost, snapshot))
            del entries[:-self.grid_cell_capacity]
        else:
            for entries in self.pareto_archive_grid.values():
                existing_cost, existing_solution = entries[0]
                if self._dominates(existing_cost, cost):
                    return
                if existing_cost == cost and all(set(a) == set(b) for a, b in zip(existing_solution, snapshot)):
                    return
            for key in list(self.pareto_archive_grid):
                if self._dominates(cost, key) or key == cost:
                    del self.pareto_archive_grid[key]
            self.pareto_archive_grid[cost] = [(cost, snapshot)]

    def run_annealing(self, T_max=500.0, T_min=0.1, gamma=0.95, steps_per_T=100,
                      hierarchy_aware_move=True):
        self.validate_schedule(T_max, T_min, gamma, steps_per_T)
        temperature = T_max
        tree = self.hierarchy_tree
        levels = sorted(tree.nodes_by_level)
        rounds = 0
        while temperature > T_min:
            temp_ratio = (temperature - T_min) / (T_max - T_min)
            weights = []
            for level in levels:
                depth = (level - 1) / (tree.max_depth - 1) if tree.max_depth > 1 else 0.5
                weights.append(0.1 + (1 - depth) * temp_ratio + depth * (1 - temp_ratio))
            for _ in range(steps_per_T):
                if hierarchy_aware_move:
                    level = self.rng.choices(levels, weights=weights, k=1)[0]
                    node = self.rng.choice(tree.nodes_by_level[level])
                else:
                    node = self.rng.choice(tree.leaves)
                delta_cut, delta_area = self.calculate_deltas(node)
                source = self.partitions[node.macro_ids[0]]
                areas = self.current_area_balance.copy()
                areas[source] -= delta_area
                areas[1 - source] += delta_area
                new_cost = (self.current_total_cutsize + delta_cut, abs(areas[1] - areas[0]))
                self._update_norm_bounds(*new_cost)
                current_norm = self._get_normalized_cost(self.current_total_cutsize, self.current_imbalance)
                new_norm = self._get_normalized_cost(*new_cost)
                # Better, equal and incomparable proposals are accepted. Only
                # dominated proposals use the reference's Metropolis rule.
                worse = self._dominates(current_norm, new_norm)
                accept = not worse
                if worse:
                    delta_cost = sum(new_norm) - sum(current_norm)
                    accept = math.exp(-delta_cost / (temperature / T_max)) > self.rng.random()
                if accept:
                    self.commit_move(node, delta_cut, delta_area)
                    self._update_pareto_archive(new_cost, self.current_solution)
            temperature *= gamma
            rounds += 1
            if rounds % 20 == 0:
                LOG.info("Temperature %.6g; cut %d; area imbalance %.6g; archive cells %d",
                         temperature, self.current_total_cutsize, self.current_imbalance,
                         len(self.pareto_archive_grid))
        return self.pareto_archive_grid

    def candidate_document(self):
        records = {}
        for cell, entries in sorted(self.pareto_archive_grid.items()):
            for index, (cost, solution) in enumerate(entries):
                # Recompute published metrics independently, so every number
                # describes the exported assignment, without accumulation drift.
                cut, imbalance, _ = self.G.metrics(solution)
                if cut != cost[0] or not math.isclose(imbalance, cost[1], rel_tol=1e-9,
                                                     abs_tol=1e-9 * max(1.0, self.G.total_area)):
                    raise RuntimeError("Incremental SA metrics disagree with the exported solution")
                records[f"{cell}:{index}"] = {
                    "cost": [cut, imbalance],
                    "solution": [[self.G.nodes[i].name for i in sorted(group)] for group in solution],
                }
        description = ("Grid archive of accepted SA states; may include dominated or duplicate solutions."
                       if self.grid_based_multi_objective else "Non-dominated SA solutions (one per cost pair).")
        return {"pareto_archive": {"description": description, "solutions": records}}


def plotting_module():
    """Load plotting only when requested, with no graphical display needed."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError(
            "Plotting requires Matplotlib. Install matplotlib in this Python environment, "
            "or use --no-plot to generate partitions without figures."
        ) from error
    return plt


def make_partition_plot(records):
    """Plot every archive entry, including overlapping and dominated states."""
    costs = [record["cost"] for record in records.values()]
    if not costs or any(not math.isfinite(value) or value < 0 for cost in costs for value in cost):
        raise ValueError("Plot requires nonempty, finite, nonnegative cut and imbalance costs")
    plt = plotting_module()
    from matplotlib.ticker import MaxNLocator

    with plt.rc_context({"font.size": 12, "pdf.fonttype": 42, "svg.fonttype": "none"}):
        figure, axis = plt.subplots(figsize=(12, 7))
        axis.scatter([cost[0] for cost in costs], [cost[1] for cost in costs],
                     s=100, color="#2b83ba", edgecolors="#263238", linewidths=0.85,
                     alpha=0.85, zorder=3)
        axis.set_title("Partition Solutions: Cut Size vs Area Imbalance", fontsize=20, fontweight="bold", pad=12)
        axis.set_xlabel("Cut Size", fontsize=16, fontweight="bold")
        axis.set_ylabel("Area Imbalance (µm²)", fontsize=16, fontweight="bold")
        axis.xaxis.set_major_locator(MaxNLocator(integer=True))
        axis.ticklabel_format(axis="x", style="plain", useOffset=False)
        axis.ticklabel_format(axis="y", style="sci", scilimits=(0, 0), useOffset=False, useMathText=True)
        axis.set_axisbelow(True)
        axis.grid(True, alpha=0.3, linewidth=0.8)
        axis.margins(x=0.045, y=0.07)
        axis.text(0.02, 0.98, f"Points: {len(costs)}", transform=axis.transAxes,
                  ha="left", va="top", fontsize=12,
                  bbox={"boxstyle": "round,pad=0.3", "facecolor": "#fff1da", "edgecolor": "#777777", "alpha": 0.9})
        figure.tight_layout(pad=1.3)
    return figure


def plot_partition_solutions(candidates, output_dir=None):
    """Read exported costs and save PNG/vector PDF without running any EDA/SA."""
    candidates = Path(candidates).resolve()
    records = exporter.read_candidate_records(candidates)
    output_dir = Path(output_dir).resolve() if output_dir is not None else candidates.parent
    figure = make_partition_plot(records)
    plt = plotting_module()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        # Render both formats before replacing any previous plot.
        with tempfile.TemporaryDirectory(prefix=".sa-plot-", dir=output_dir) as temporary:
            for name in PLOT_FILES:
                figure.savefig(Path(temporary) / name, dpi=300, bbox_inches="tight", facecolor="white")
            for name in PLOT_FILES:
                os.replace(Path(temporary) / name, output_dir / name)
    finally:
        plt.close(figure)
    paths = tuple(output_dir / name for name in PLOT_FILES)
    LOG.info("Plotted %d archive entries: %s", len(records), paths[0])
    return paths


def export_graph(seed_db, graph_path, log_path, net_pin_limit=10):
    """Load OpenDB in OpenROAD's runtime, then parse its tool-independent TSV."""
    executable = os.environ.get("OPENROAD_EXE") or shutil.which("openroad")
    if not executable:
        raise RuntimeError("OpenROAD is unavailable; set OPENROAD_EXE or update PATH")
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sa-graph-", dir=graph_path.parent) as temporary:
        temporary_graph = Path(temporary) / "sa_graph.tsv"
        environment = {**os.environ, "SA_SEED_DB": str(seed_db), "SA_GRAPH_FILE": str(temporary_graph)}
        command = [executable, "-no_init", "-exit", str(REPO_ROOT / "scripts_openroad" / "export_sa_graph.tcl")]
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=REPO_ROOT, env=environment,
                                    stdout=log, stderr=subprocess.STDOUT, check=False)
        if result.returncode or not temporary_graph.is_file():
            tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
            raise RuntimeError(f"OpenDB graph export failed (exit {result.returncode}). Log: {log_path}\n{tail}")
        graph = graph_construction(temporary_graph, net_pin_limit)
        os.replace(temporary_graph, graph_path)
    return graph


def read_cache_metadata(path):
    try:
        data = exporter.read_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_or_export_graph(seed_db, graph_path, log_path, net_pin_limit=10, *,
                         previous_candidates=None, rebuild=False):
    """Cache the raw OpenDB export independently of all SA/graph parameters.

    The TSV includes every signal net, so even a changed net-pin cutoff only
    needs to rebuild the in-memory adjacency, without running OpenROAD again.
    """
    script = REPO_ROOT / "scripts_openroad" / "export_sa_graph.tcl"
    identity = {"seed_odb_sha256": exporter.file_sha256(seed_db),
                "exporter_sha256": exporter.file_sha256(script)}
    state_path = graph_path.with_name("sa_graph_info.json")
    state = read_cache_metadata(state_path)
    graph_sha = exporter.file_sha256(graph_path) if graph_path.is_file() else None
    reusable = (not rebuild and graph_sha is not None
                and all(state.get(key) == value for key, value in identity.items())
                and state.get("graph_sha256") == graph_sha)
    # Adopt graphs made by the first version of this script. Its archive
    # already records both hashes; the exporter must not have changed since.
    if not rebuild and graph_sha is not None and not state_path.exists() and previous_candidates is not None:
        previous = read_cache_metadata(previous_candidates).get("run", {})
        reusable = (isinstance(previous, dict) and previous.get("graph_sha256") == graph_sha
                    and previous.get("seed_odb_sha256") == identity["seed_odb_sha256"]
                    and script.stat().st_mtime_ns <= graph_path.stat().st_mtime_ns)
    if reusable:
        LOG.info("[prepare] Reusing cached OpenDB graph: %s", graph_path)
        graph = graph_construction(graph_path, net_pin_limit)
    else:
        LOG.info("[prepare] Exporting OpenDB graph; log: %s", log_path)
        graph = export_graph(seed_db, graph_path, log_path, net_pin_limit)
        graph_sha = exporter.file_sha256(graph_path)
    exporter.write_if_changed(state_path, json.dumps({**identity, "graph_sha256": graph_sha}, indent=2) + "\n")
    return graph


def import_seed_results(source, preparation_dir):
    """Copy a complete existing 2D seed; never silently replace another seed."""
    sources = [source / name for name in SEED_FILES]
    missing = [str(path) for path in sources if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise ValueError(f"Incomplete 2D seed results: {missing}")
    destination = preparation_dir / "results"
    for path in sources:
        target = destination / path.name
        if target.is_file() and exporter.file_sha256(target) != exporter.file_sha256(path):
            raise ValueError(f"Existing preparation contains a different seed: {target}; use a fresh preparation directory")
    destination.mkdir(parents=True, exist_ok=True)
    for path in sources:
        target = destination / path.name
        # Existing targets were checked above; identical seeds need no copying.
        if target.is_file():
            continue
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copy2(path, temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()


def replace_partition_outputs(output_dir, files):
    """Refresh generated partitions and remove only obsolete generated files."""
    expected_ids = {Path(name).parts[1] for name in files if name.startswith("solutions/")}
    solutions_dir = output_dir / "solutions"
    stale = [directory for directory in solutions_dir.iterdir()
             if directory.is_dir() and not directory.is_symlink()
             and re.fullmatch(r"s\d+", directory.name) and directory.name not in expected_ids
             ] if solutions_dir.is_dir() else []
    # All candidate assignments have already passed the existing exporter's
    # validation before any old partition is replaced or removed.
    for relative, content in files.items():
        if relative.startswith("solutions/"):
            exporter.write_if_changed(output_dir / relative, content)
    for directory in stale:
        for name in ("macro_partition.txt", "solution.json"):
            path = directory / name
            if path.is_file():
                path.unlink()
        if not any(directory.iterdir()):
            directory.rmdir()
    for relative, content in files.items():
        if not relative.startswith("solutions/"):
            exporter.write_if_changed(output_dir / relative, content)
    LOG.info("Exported %d solutions; removed generated files from %d obsolete solution folders",
             len(expected_ids), len(stale))


def generate(design_config, output_dir=None, *, preparation_dir=None, seed_results=None,
             rebuild_seed=False, seed=42, grid_size=40, grid_cell_capacity=3,
             grid_based_multi_objective=True, hierarchy_aware_move=True,
             hierarchy_separator=None, net_pin_limit=10,
             T_max=500.0, T_min=0.1, gamma=0.95, steps_per_T=100, make_plots=True):
    HMSA.validate_schedule(T_max, T_min, gamma, steps_per_T)
    if grid_size < 1 or grid_cell_capacity < 1 or net_pin_limit < 2:
        raise ValueError("Positive grid size/capacity and a net pin limit of at least 2 are required")
    if hierarchy_separator is not None and (not hierarchy_separator or any(c.isspace() for c in hierarchy_separator)):
        raise ValueError("hierarchy separator must be nonempty and contain no whitespace")
    config = Path(design_config).resolve()
    if not config.is_file():
        raise ValueError(f"2D design config not found: {config}")
    if seed_results is not None and rebuild_seed:
        raise ValueError("--seed-results and --rebuild-seed cannot be combined")
    if make_plots:
        plotting_module()  # Report missing dependencies before preparing or replacing outputs.
    output_dir = (Path(output_dir).resolve() if output_dir is not None else
                  REPO_ROOT / "partition_experiments_sa" / config.parent.parent.name / config.parent.name)
    preparation_dir = Path(preparation_dir).resolve() if preparation_dir is not None else output_dir / "preparation"
    candidates = output_dir / "candidates.json"

    with exporter.preparation_lifecycle_lock(preparation_dir):
        if seed_results is None:
            masters = exporter.prepare_macro_inventory(config, preparation_dir, rebuild_seed)
        else:
            import_seed_results(Path(seed_results).resolve(), preparation_dir)
            masters = None
        graph_path = preparation_dir / "results" / "sa_graph.tsv"
        graph = load_or_export_graph(
            preparation_dir / "results" / "2_2_floorplan_io.odb", graph_path,
            preparation_dir / "logs" / "sa_graph.log", net_pin_limit,
            previous_candidates=candidates, rebuild=rebuild_seed,
        )
        graph_masters = {node.name: node.master for node in graph.nodes.values() if node.is_macro}
        if masters is not None and masters != graph_masters:
            raise ValueError("SA graph macro inventory differs from the prepared 2D seed")
        masters = graph_masters
        if seed_results is not None:
            inventory = preparation_dir / "macro_names.tsv"
            exporter.write_if_changed(inventory, exporter.tsv_text([
                ["instance_name", "master_name"], *sorted(masters.items()),
            ]))
            state = {
                "status": "complete", "design_config": str(config),
                "config_sha256": exporter.file_sha256(config), "macro_count": len(masters),
                "inventory_sha256": exporter.file_sha256(inventory),
                "seed_sha256": {name: exporter.file_sha256(preparation_dir / "results" / name) for name in SEED_FILES},
                "seed_source": str(Path(seed_results).resolve()),
            }
            exporter.write_if_changed(preparation_dir / "seed_info.json", json.dumps(state, indent=2) + "\n")
        LOG.info("Loaded %d instances (%d macros), %d clique edges", len(graph.nodes), len(masters),
                 sum(sum(neighbors.values()) for neighbors in graph.adjacency.values()) // 2)
        optimizer = HMSA(graph, seed=seed, grid_size=grid_size, grid_cell_capacity=grid_cell_capacity,
                         grid_based_multi_objective=grid_based_multi_objective, hierarchy_separator=hierarchy_separator)
        LOG.info("Running SA with seed %d, grid size %d, and %d steps per temperature",
                 seed, grid_size, steps_per_T)
        optimizer.run_annealing(T_max, T_min, gamma, steps_per_T, hierarchy_aware_move)
        document = optimizer.candidate_document()
        document["run"] = {
            "algorithm": "TaiWei hierarchy-aware multi-objective simulated annealing",
            "design_config": str(config), "graph_sha256": exporter.file_sha256(graph_path),
            "seed_odb_sha256": exporter.file_sha256(preparation_dir / "results" / "2_2_floorplan_io.odb"),
            "standard_cell_tier": "bottom", "area_unit": "um^2", "cut_metric": "clique edge multiplicity",
            "seed": seed, "grid_size": grid_size, "grid_cell_capacity": grid_cell_capacity,
            "grid_based_multi_objective": grid_based_multi_objective, "hierarchy_aware_move": hierarchy_aware_move,
            "hierarchy_separator": hierarchy_separator if hierarchy_separator is not None else graph.hierarchy_separator,
            "net_pin_limit_exclusive": net_pin_limit, "T_max": T_max, "T_min": T_min,
            "gamma": gamma, "steps_per_T": steps_per_T,
        }
        text = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".sa-export-", dir=output_dir) as temporary:
            staged = Path(temporary) / "candidates.json"
            staged.write_text(text, encoding="utf-8")
            files, count = exporter.prepare_files(staged, {}, masters)
        exporter.export_cc_seed(preparation_dir, output_dir)
        files["candidates.json"] = text
        files["hierarchy_tree.txt"] = optimizer.hierarchy_tree.as_text()
        replace_partition_outputs(output_dir, files)
        if make_plots:
            plot_partition_solutions(candidates, output_dir)
    return count, output_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("design_config", type=Path, nargs="?", help="TaiWei 2D config (config2d.mk); omitted with --plot-only")
    parser.add_argument("--plot-only", type=Path, metavar="CANDIDATES",
                        help="Plot an existing candidates.json without running preparation or SA")
    parser.add_argument("--no-plot", action="store_true", help="Skip plotting; Matplotlib is not required")
    parser.add_argument("--output-dir", type=Path,
                        help="Default: partition_experiments_sa/<platform>/<case>; reruns replace generated partitions")
    parser.add_argument("--preparation-dir", type=Path, help="Reusable 2D preparation cache (default: <output-dir>/preparation)")
    parser.add_argument("--seed-results", type=Path, help="Import an existing matching 2D results directory, including its ODB")
    parser.add_argument("--rebuild-seed", action="store_true", help="Rebuild after RTL/library changes")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--grid-size", type=int, default=25)
    parser.add_argument("--grid-cell-capacity", type=int, default=3)
    parser.add_argument("--disable-grid-based-multi-objective", action="store_true", help="Retain only the true Pareto front")
    parser.add_argument("--disable-hierarchy-aware-move", action="store_true", help="Move a uniformly random single macro")
    parser.add_argument("--hierarchy-separator", help="Override the OpenDB hierarchy delimiter (e.g. __ for flattened names)")
    parser.add_argument("--net-pin-limit", type=int, default=10, help="Only expand nets with fewer pins than this value (default: 10)")
    parser.add_argument("--t-max", type=float, default=500.0)
    parser.add_argument("--t-min", type=float, default=0.1)
    parser.add_argument("--gamma", type=float, default=0.95)
    parser.add_argument("--steps-per-temperature", type=int, default=100)
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    if args.plot_only is not None and (args.design_config is not None or args.no_plot):
        parser.error("Use --plot-only without a design config or --no-plot")
    if args.plot_only is None and args.design_config is None:
        parser.error("Provide a 2D design config or --plot-only CANDIDATES")
    try:
        if args.plot_only is not None:
            for path in plot_partition_solutions(args.plot_only, args.output_dir):
                print(f"Saved plot: {path}")
            return 0
        count, directory = generate(
            args.design_config, args.output_dir, preparation_dir=args.preparation_dir,
            seed_results=args.seed_results, rebuild_seed=args.rebuild_seed, seed=args.seed,
            grid_size=args.grid_size, grid_cell_capacity=args.grid_cell_capacity,
            grid_based_multi_objective=not args.disable_grid_based_multi_objective,
            hierarchy_aware_move=not args.disable_hierarchy_aware_move,
            hierarchy_separator=args.hierarchy_separator, net_pin_limit=args.net_pin_limit,
            T_max=args.t_max, T_min=args.t_min, gamma=args.gamma, steps_per_T=args.steps_per_temperature,
            make_plots=not args.no_plot,
        )
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")
    print(f"Generated {count} solution folders under {directory / 'solutions'}")
    print(f"2D seed inputs for CC evaluation: {directory / 'preparation' / 'results'}")
    if not args.no_plot:
        for name in PLOT_FILES:
            print(f"Saved plot: {directory / name}")
    print("Costs: [clique cut size, absolute tier area difference in um^2]; standard cells stay on bottom.")
    print("Physical tier labels: 0 = upper; 1 = bottom.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
