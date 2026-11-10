#!/usr/bin/env python3
"""Run Place-MoL min-cut, max-cut and GNN-cut on a TaiWei 2D preparation seed.

    python3 generate_placemol_macro_partitions.py designs/nangate45_3D/bp_be/config2d.mk

Reuse an existing preparation with --preparation-dir or import its results with
--seed-results. Exports the same solutions/, manifest.tsv, macro_name_map.tsv
and preparation/results/ CC handoff as generate_sa_macro_partitions.py, plus
the reference metrics and optional cut/imbalance plots. No 3D jobs are launched.
"""

import argparse
import json
import logging
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile

import generate_macro_partitions as exporter
import generate_sa_macro_partitions as sa_flow
from partition_algorithms.placemol.opendb_adapter import make_problem_instance, read_snapshot


REPO_ROOT = Path(__file__).resolve().parent
METHODS = ("min-cut", "max-cut", "gnn-cut")
LOG = logging.getLogger(__name__)


def dependencies(methods, device):
    """Fail before EDA preparation if a requested algorithm cannot run."""
    try:
        from partition_algorithms.placemol.graph_builder import GraphBuilder
        from partition_algorithms.placemol.graph_cut import GraphCutPartitioner
        if "gnn-cut" in methods:
            import torch
            from partition_algorithms.placemol.GNN_partition import train_gnn_partition
            if device != "cpu":
                if not device.startswith("cuda") or not torch.cuda.is_available():
                    raise ValueError("Use --device cpu or an available CUDA device")
                # Validate the requested CUDA index before preparation.
                torch.empty(0, device=device)
    except ImportError as error:
        raise RuntimeError(
            "Place-MoL min/max cut requires numpy; GNN-cut also requires torch, "
            "torch-geometric and scikit-learn. Install requirements-placemol.txt "
            "in the Python environment running this script. " + str(error)
        ) from error
    return GraphBuilder, GraphCutPartitioner


def validate_options(method, num_runs, seed, scale_factor, top_k_ratio, k_hop,
                     area_balance_error, gnn_options, threads):
    if method not in (*METHODS, "all"):
        raise ValueError(f"Unknown partition method: {method}")
    if num_runs < 1 or not 0 <= seed < 2**32 or seed + num_runs > 2**32:
        raise ValueError("Require positive num-runs and run seeds within [0, 2**32)")
    if not math.isfinite(scale_factor) or scale_factor <= 0:
        raise ValueError("scale-factor must be finite and positive")
    if not math.isfinite(top_k_ratio) or not 0 <= top_k_ratio <= 1:
        raise ValueError("top-k-ratio must be in [0, 1]; 0 disables edge filtering")
    if k_hop < 1 or threads < 1:
        raise ValueError("k-hop and threads must be positive integers")
    if not math.isfinite(area_balance_error) or not 0 <= area_balance_error <= 1:
        raise ValueError("area-balance-error must be in [0, 1]")
    for name in ("num_epochs", "hidden_dim", "embedding_dim", "num_layers"):
        if gnn_options[name] < 1:
            raise ValueError(f"GNN {name} must be positive")
    for name in ("lr", "temperature"):
        if not math.isfinite(gnn_options[name]) or gnn_options[name] <= 0:
            raise ValueError(f"GNN {name} must be finite and positive")
    if not math.isfinite(gnn_options["dropout"]) or not 0 <= gnn_options["dropout"] < 1:
        raise ValueError("GNN dropout must be in [0, 1)")


def export_snapshot(seed_db, graph_path, log_path):
    executable = os.environ.get("OPENROAD_EXE") or shutil.which("openroad")
    if not executable:
        raise RuntimeError("OpenROAD is unavailable; set OPENROAD_EXE or update PATH")
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".placemol-graph-", dir=graph_path.parent) as temporary:
        snapshot = Path(temporary) / "placemol_graph.tsv"
        environment = {**os.environ, "PLACEMOL_SEED_DB": str(seed_db), "PLACEMOL_GRAPH_FILE": str(snapshot)}
        command = [executable, "-no_init", "-exit", str(REPO_ROOT / "scripts_openroad/export_placemol_graph.tcl")]
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=REPO_ROOT, env=environment,
                                    stdout=log, stderr=subprocess.STDOUT, check=False)
        if result.returncode or not snapshot.is_file():
            tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
            raise RuntimeError(f"OpenDB graph export failed (exit {result.returncode}). Log: {log_path}\n{tail}")
        design = read_snapshot(snapshot)
        os.replace(snapshot, graph_path)
    return design


def load_or_export_snapshot(seed_db, graph_path, log_path, rebuild=False):
    """Cache raw geometry/pins independently of every partition parameter."""
    script = REPO_ROOT / "scripts_openroad/export_placemol_graph.tcl"
    identity = {"seed_odb_sha256": exporter.file_sha256(seed_db), "exporter_sha256": exporter.file_sha256(script)}
    state_path = graph_path.with_name("placemol_graph_info.json")
    state = sa_flow.read_cache_metadata(state_path)
    graph_sha = exporter.file_sha256(graph_path) if graph_path.is_file() else None
    if (not rebuild and graph_sha is not None and state.get("graph_sha256") == graph_sha
            and all(state.get(key) == value for key, value in identity.items())):
        LOG.info("[prepare] Reusing OpenDB snapshot: %s", graph_path)
        design = read_snapshot(graph_path)
    else:
        LOG.info("[prepare] Exporting OpenDB snapshot; log: %s", log_path)
        design = export_snapshot(seed_db, graph_path, log_path)
        graph_sha = exporter.file_sha256(graph_path)
    exporter.write_if_changed(state_path, json.dumps({**identity, "graph_sha256": graph_sha}, indent=2) + "\n")
    return design


def partition_graph(graph, methods, seed, num_runs, area_balance_error, gnn_options, device, threads):
    import numpy as np
    from partition_algorithms.placemol.graph_cut import GraphCutPartitioner

    partitioner = GraphCutPartitioner(graph, area_balance_error)
    records = {}
    for run in range(num_runs):
        run_seed = seed + run
        for method in methods:
            LOG.info("Running Place-MoL %s, seed %d", method, run_seed)
            history = None
            if method == "gnn-cut":
                if graph.n_macros < 2:
                    raise ValueError("GNN-cut KMeans requires at least two macros")
                import torch
                from threadpoolctl import threadpool_limits
                from partition_algorithms.placemol.GNN_partition import train_gnn_partition, partition_with_kmeans
                random.seed(run_seed)
                np.random.seed(run_seed)
                torch.manual_seed(run_seed)
                if torch.cuda.is_available():
                    torch.cuda.manual_seed_all(run_seed)
                torch.set_num_threads(threads)
                with threadpool_limits(limits=threads):
                    model, history = train_gnn_partition(graph, device=device, **gnn_options)
                    if not all(math.isfinite(value) for value in history):
                        raise RuntimeError("GNN training produced a non-finite loss")
                    selected = partition_with_kmeans(model, graph, device=device, area_balance_error=area_balance_error)
            else:
                selected = getattr(partitioner, method.replace("-", "_"))(seed=run_seed)
            # Publish the reference evaluator, including its macro-only cut.
            metrics = {key: value.item() if hasattr(value, "item") else value
                       for key, value in partitioner.evaluate_cut(selected).items()}
            if not all(math.isfinite(value) for value in metrics.values()):
                raise RuntimeError("Partition evaluation produced a non-finite metric")
            if not metrics["is_balanced"]:
                LOG.warning("%s seed %d: reference result exceeds the area tolerance (ratio %.6g > %.6g)",
                            method, run_seed, metrics["area_imbalance_ratio"], area_balance_error)
            bottom = set(selected)
            groups = [[graph.problem_instance.dmp_placedb.node_names[graph.graph_idx_to_placedb_macro_id[i]]
                       for i in range(graph.n_macros) if (i in bottom) == is_bottom]
                      for is_bottom in (True, False)]
            record = {"method": method, "seed": run_seed,
                      "cost": [metrics["cut_weight"], metrics["area_diff"]],
                      "metrics": metrics, "solution": groups}
            if history is not None:
                record["loss_history"] = history
            records[f"{method}:seed{run_seed}"] = record
    return records


def generate(design_config, output_dir=None, *, preparation_dir=None, seed_results=None,
             rebuild_seed=False, method="all", seed=42, num_runs=1, scale_factor=1.0,
             top_k_ratio=0.3, k_hop=1, area_balance_error=0.1,
             num_epochs=20, lr=0.001, hidden_dim=128, embedding_dim=64,
             num_layers=3, dropout=0.1, temperature=0.07, device="cpu", threads=1, make_plots=True):
    gnn_options = dict(num_epochs=num_epochs, lr=lr, hidden_dim=hidden_dim, embedding_dim=embedding_dim,
                       num_layers=num_layers, dropout=dropout, temperature=temperature)
    validate_options(method, num_runs, seed, scale_factor, top_k_ratio, k_hop,
                     area_balance_error, gnn_options, threads)
    methods = METHODS if method == "all" else (method,)
    GraphBuilder, _ = dependencies(methods, device)
    config = Path(design_config).resolve()
    if not config.is_file():
        raise ValueError(f"2D design config not found: {config}")
    if seed_results is not None and rebuild_seed:
        raise ValueError("--seed-results and --rebuild-seed cannot be combined")
    if make_plots:
        sa_flow.plotting_module()
    output_dir = (Path(output_dir).resolve() if output_dir is not None else
                  REPO_ROOT / "partition_experiments_placemol" / config.parent.parent.name / config.parent.name)
    preparation_dir = Path(preparation_dir).resolve() if preparation_dir is not None else output_dir / "preparation"
    with exporter.preparation_lifecycle_lock(preparation_dir):
        if seed_results is None:
            masters = exporter.prepare_macro_inventory(config, preparation_dir, rebuild_seed)
        else:
            sa_flow.import_seed_results(Path(seed_results).resolve(), preparation_dir)
            masters = None
        graph_path = preparation_dir / "results/placemol_graph.tsv"
        instances, nets = load_or_export_snapshot(
            preparation_dir / "results/2_2_floorplan_io.odb", graph_path,
            preparation_dir / "logs/placemol_graph.log", rebuild_seed,
        )
        graph_masters = {node.name: node.master for node in instances if node.kind == "macro"}
        if masters is not None and masters != graph_masters:
            raise ValueError("Place-MoL graph macro inventory differs from the prepared 2D seed")
        masters = graph_masters
        if "gnn-cut" in methods and len(masters) < 2:
            raise ValueError("GNN-cut KMeans requires at least two macros")
        if seed_results is not None:
            inventory = preparation_dir / "macro_names.tsv"
            exporter.write_if_changed(inventory, exporter.tsv_text([
                ["instance_name", "master_name"], *sorted(masters.items()),
            ]))
            state = {"status": "complete", "design_config": str(config),
                     "config_sha256": exporter.file_sha256(config), "macro_count": len(masters),
                     "inventory_sha256": exporter.file_sha256(inventory),
                     "seed_sha256": {name: exporter.file_sha256(preparation_dir / "results" / name)
                                     for name in sa_flow.SEED_FILES},
                     "seed_source": str(Path(seed_results).resolve())}
            exporter.write_if_changed(preparation_dir / "seed_info.json", json.dumps(state, indent=2) + "\n")
        problem = make_problem_instance(instances, nets, scale_factor=scale_factor, top_k_ratio=top_k_ratio, k_hop=k_hop)
        graph = GraphBuilder(problem)
        LOG.info("Loaded %d macros + merged cell node; %d edges", graph.n_macros, len(graph.edges))
        records = partition_graph(graph, methods, seed, num_runs, area_balance_error, gnn_options, device, threads)
        document = {
            "pareto_archive": {"description": "Place-MoL algorithm outputs; no Pareto filtering or deduplication.",
                               "solutions": records},
            "run": {"algorithm": "Open3DBench Place-MoL reference partition algorithms",
                    "design_config": str(config), "methods": list(methods), "seed": seed, "num_runs": num_runs,
                    "graph_sha256": exporter.file_sha256(graph_path),
                    "seed_odb_sha256": exporter.file_sha256(preparation_dir / "results/2_2_floorplan_io.odb"),
                    "standard_cell_tier": "bottom", "area_unit": "um^2",
                    "cost_fields": ["cut_weight", "area_diff"], "cut_metric": "macro-to-macro edge weights only",
                    "scale_factor": scale_factor, "top_k_ratio": top_k_ratio, "k_hop": k_hop,
                    "area_balance_error": area_balance_error, "gnn": {**gnn_options, "device": device, "threads": threads},
                    "macro_count": graph.n_macros, "edge_count": len(graph.edges)},
        }
        text = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".placemol-export-", dir=output_dir) as temporary:
            staged = Path(temporary) / "candidates.json"
            staged.write_text(text, encoding="utf-8")
            files, count = exporter.prepare_files(staged, {}, masters)
        metric_columns = ("cut_weight", "bottom_area", "upper_area", "area_diff", "area_imbalance_ratio", "is_balanced")
        metric_rows = [["solution_id", "method", "seed", *metric_columns]]
        for index, record in enumerate(records.values()):
            solution_id = f"s{index:04d}"
            relative = f"solutions/{solution_id}/solution.json"
            metadata = json.loads(files[relative])
            metadata.update(method=record["method"], seed=record["seed"], metrics=record["metrics"])
            files[relative] = json.dumps(metadata, indent=2, allow_nan=False) + "\n"
            metric_rows.append([solution_id, record["method"], record["seed"],
                                *[record["metrics"][column] for column in metric_columns]])
        files["metrics.tsv"] = exporter.tsv_text(metric_rows)
        files["candidates.json"] = text
        exporter.export_cc_seed(preparation_dir, output_dir)
        sa_flow.replace_partition_outputs(output_dir, files)
        if make_plots:
            sa_flow.plot_partition_solutions(output_dir / "candidates.json", output_dir)
    return count, output_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("design_config", type=Path, help="TaiWei 2D config (config2d.mk)")
    parser.add_argument("--method", choices=(*METHODS, "all"), default="all")
    parser.add_argument("--output-dir", type=Path, help="Default: partition_experiments_placemol/<platform>/<case>")
    parser.add_argument("--preparation-dir", type=Path, help="Reuse a TaiWei 2D preparation directory")
    parser.add_argument("--seed-results", type=Path, help="Import matching 2D results (SDC, DEF, Verilog and ODB)")
    parser.add_argument("--rebuild-seed", action="store_true")
    parser.add_argument("--no-plot", action="store_true", help="Skip PNG/PDF plots; Matplotlib is optional")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-runs", type=int, default=1, help="Repeat each method with seeds seed, seed+1, ...")
    parser.add_argument("--scale-factor", type=float, default=1.0, help="Reference cell area divisor (default: 1)")
    parser.add_argument("--top-k-ratio", type=float, default=0.3, help="Reference edge filter ratio; 0 disables filtering")
    parser.add_argument("--k-hop", type=int, default=1)
    parser.add_argument("--area-balance-error", type=float, default=0.1, help="Maximum absolute area difference / total area")
    parser.add_argument("--gnn-epochs", type=int, default=200)
    parser.add_argument("--gnn-learning-rate", type=float, default=0.001)
    parser.add_argument("--gnn-hidden-dim", type=int, default=128)
    parser.add_argument("--gnn-embedding-dim", type=int, default=64)
    parser.add_argument("--gnn-num-layers", type=int, default=3)
    parser.add_argument("--gnn-dropout", type=float, default=0.1)
    parser.add_argument("--gnn-temperature", type=float, default=0.07)
    parser.add_argument("--device", default="cpu", help="GNN device: cpu or cuda[:index]")
    parser.add_argument("--threads", type=int, default=1, help="PyTorch and KMeans CPU thread limit")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        count, directory = generate(
            args.design_config, args.output_dir, preparation_dir=args.preparation_dir,
            seed_results=args.seed_results, rebuild_seed=args.rebuild_seed, method=args.method,
            seed=args.seed, num_runs=args.num_runs, scale_factor=args.scale_factor,
            top_k_ratio=args.top_k_ratio, k_hop=args.k_hop, area_balance_error=args.area_balance_error,
            num_epochs=args.gnn_epochs, lr=args.gnn_learning_rate, hidden_dim=args.gnn_hidden_dim,
            embedding_dim=args.gnn_embedding_dim, num_layers=args.gnn_num_layers,
            dropout=args.gnn_dropout, temperature=args.gnn_temperature, device=args.device,
            threads=args.threads, make_plots=not args.no_plot,
        )
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")
    print(f"Generated {count} solution folders under {directory / 'solutions'}")
    print(f"2D seed inputs for CC evaluation: {directory / 'preparation/results'}")
    print("Costs: [reference macro-only cut weight, absolute area difference in um^2].")
    print("Physical tier labels: 0 = upper; 1 = bottom. Keep STD_CELL_TIER=bottom.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
