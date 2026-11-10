#!/usr/bin/env python3
"""Plot partition geometry colored by a final PPA metric and rank closest to zero."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sys


@dataclass(frozen=True)
class Metric:
    column: str
    label: str
    unit: str
    tick_format: str | None = "%.2f"
    cmap: str = "coolwarm_r"


METRICS = {
    "wns": Metric("wns", "WNS", "ns"),
    "tns": Metric("tns", "TNS", "ns"),
    "rwl": Metric("after_routing_wirelength", "rWL", "µm", None, "coolwarm"),
    "power": Metric("power", "Power", "W", "%.3f", "coolwarm"),
}
MERGE_METRICS = ("wns", "tns", "rwl", "power")
PLOT_STYLE = {
    "font.size": 13, "axes.labelsize": 17, "axes.labelweight": "bold",
    "pdf.fonttype": 42, "svg.fonttype": "none",
}


@dataclass(frozen=True)
class Solution:
    idx: int
    cut_size: float
    area_imbalance: float
    value: float | None
    cost: float | None


def finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label}: expected a finite number, got {value!r}")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{label}: expected a finite number, got {value!r}") from error
    if not math.isfinite(number):
        raise ValueError(f"{label}: expected a finite number, got {value!r}")
    return number


def optional_number(value: str | None, label: str) -> float | None:
    if value is None or value.strip().upper() in ("", "N/A"):
        return None
    return finite_number(value, label)


def read_metrics(csv_path: Path, metric: str = "wns") -> dict[int, tuple[float | None, float | None]]:
    spec = METRICS[metric]
    metrics = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"idx", spec.column}.issubset(reader.fieldnames or []):
            raise ValueError(f"{csv_path}: expected idx and {spec.column} columns")
        for row in reader:
            label = f"{csv_path}:{reader.line_num}"
            match = re.fullmatch(r"s?(\d+)", (row.get("idx") or "").strip())
            if not match:
                raise ValueError(f"{label}: invalid partition index {row.get('idx')!r}")
            idx = int(match.group(1))
            if idx in metrics:
                raise ValueError(f"{label}: duplicate partition index {idx}")
            metrics[idx] = (
                optional_number(row.get(spec.column), f"{label} {spec.label}"),
                optional_number(row.get("cost"), f"{label} PPA cost"),
            )
    return metrics


def load_solutions(solutions_dir: Path, csv_path: Path, metric: str = "wns") -> list[Solution]:
    metrics = read_metrics(csv_path, metric)
    solutions = []
    seen = set()
    for folder in sorted(solutions_dir.iterdir()):
        if not folder.is_dir() or not re.fullmatch(r"s\d+", folder.name):
            continue
        idx = int(folder.name[1:])
        if idx in seen:
            raise ValueError(f"{solutions_dir}: duplicate numeric solution index {idx}")
        seen.add(idx)
        path = folder / "solution.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        geometry = payload.get("cost") if isinstance(payload, dict) else None
        if not isinstance(geometry, list) or len(geometry) != 2:
            raise ValueError(f"{path}: expected cost=[cut_size, area_imbalance]")
        cut_size = finite_number(geometry[0], f"{path} cut size")
        area_imbalance = finite_number(geometry[1], f"{path} area imbalance")
        if cut_size < 0 or area_imbalance < 0:
            raise ValueError(f"{path}: cut size and area imbalance must be nonnegative")
        # JSON cost is partition geometry; CSV cost is the normalized PPA score.
        value, cost = metrics.get(idx, (None, None))
        solutions.append(Solution(idx, cut_size, area_imbalance, value, cost))
    if not solutions:
        raise ValueError(f"{solutions_dir}: no sNNNN solution folders found")
    solutions.sort(key=lambda solution: solution.idx)
    return solutions


def top_solutions(solutions: list[Solution], count: int = 5) -> list[Solution]:
    """Rank by selected metric closest to zero, breaking ties by partition index."""
    eligible = [solution for solution in solutions if solution.value is not None]
    return sorted(eligible, key=lambda solution: (abs(solution.value), solution.idx))[:count]


def balanced_min_cut(solutions: list[Solution]) -> Solution | None:
    """Choose minimum area imbalance, then minimum cut size, then index."""
    return min(
        solutions,
        key=lambda solution: (solution.area_imbalance, solution.cut_size, solution.idx),
        default=None,
    )


def make_plot(
    solutions: list[Solution], top_n: int = 5, metric: str = "wns", *,
    ax=None, show_colorbar: bool = True, show_axis_titles: bool = True,
):
    # Agg works on compute nodes and over SSH without a graphical display.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as path_effects
    from matplotlib.colors import Normalize
    from matplotlib.lines import Line2D
    from matplotlib.ticker import FormatStrFormatter, MaxNLocator, ScalarFormatter

    spec = METRICS[metric]
    measured = [solution for solution in solutions if solution.value is not None]
    if not measured:
        raise ValueError(f"no finite {spec.label} values matched the solution indices")
    ranked = top_solutions(solutions, top_n)
    balanced = balanced_min_cut(solutions)
    dots = [solution for solution in measured if solution != balanced]
    values = [solution.value for solution in measured]
    # Keep the color scale based on all measured solutions, including the star.
    norm = Normalize(vmin=min(values), vmax=max(values))
    if norm.vmin == norm.vmax:
        # Match the colorbar's constant-data expansion even when no bar is drawn.
        padding = abs(norm.vmin) * 0.1 or 0.1
        norm = Normalize(vmin=norm.vmin - padding, vmax=norm.vmax + padding)

    with plt.rc_context(PLOT_STYLE):
        if ax is None:
            fig, ax = plt.subplots(figsize=(10.5, 6.8), constrained_layout=True)
        else:
            fig = ax.figure
        ax.set_axisbelow(True)
        ax.grid(color="0.90", linewidth=0.8)
        scatter = ax.scatter(
            [solution.cut_size for solution in dots],
            [solution.area_imbalance for solution in dots],
            c=[solution.value for solution in dots], cmap=spec.cmap, norm=norm,
            s=88, edgecolors="0.25", linewidths=0.8, alpha=0.95, zorder=3,
        )
        if show_colorbar:
            colorbar = fig.colorbar(scatter, ax=ax, pad=0.035, fraction=0.05)
            colorbar.set_label(f"{spec.label} ({spec.unit})", fontsize=16, fontweight="bold")
            if spec.tick_format is not None:
                colorbar.formatter = FormatStrFormatter(spec.tick_format)
            else:
                formatter = ScalarFormatter(useMathText=True)
                formatter.set_powerlimits((0, 0))
                colorbar.formatter = formatter
            colorbar.update_ticks()

        legend_handles = []
        if balanced is not None and balanced.value is not None:
            ax.scatter(
                [balanced.cut_size], [balanced.area_imbalance],
                c=[balanced.value], cmap=spec.cmap, norm=norm, marker="*",
                s=380, edgecolors="black", linewidths=1.2, alpha=0.95, zorder=5,
            )
            legend_handles.append(Line2D(
                [], [], linestyle="none", marker="*", markersize=15,
                markerfacecolor="0.7", markeredgecolor="black",
                label="Balanced Min-Cut",
            ))
        if ranked:
            ranked_dots = [solution for solution in ranked if solution != balanced]
            if ranked_dots:
                ax.scatter(
                    [solution.cut_size for solution in ranked_dots],
                    [solution.area_imbalance for solution in ranked_dots],
                    s=148, facecolors="none", edgecolors="black", linewidths=1.6, zorder=4,
                )
            legend_handles.append(Line2D(
                [], [], linestyle="none", marker="o", markersize=10,
                markerfacecolor="none", markeredgecolor="black", markeredgewidth=1.6,
                label=f"Top {len(ranked)} by {spec.label}",
            ))
        if legend_handles:
            ax.legend(handles=legend_handles, loc="upper right", fontsize=14,
                      markerscale=1.15, framealpha=0.95, edgecolor="0.85")
        ax.set_xlabel("Cut Size" if show_axis_titles else "")
        ax.set_ylabel("Area Imbalance" if show_axis_titles else "")
        ax.xaxis.set_major_locator(MaxNLocator(nbins=7, integer=True))
        ax.ticklabel_format(axis="y", style="sci", scilimits=(0, 0), useMathText=True)
        ax.margins(x=0.06, y=0.10)
        ax.set_ylim(bottom=0)

        # Place only rank labels, keeping their rendered boxes from overlapping.
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        label_boxes = []
        offsets = ((0, 14), (16, 14), (-16, 14), (0, -22),
                   (22, 0), (-22, 0), (26, 28), (-26, 28), (0, 36))
        for rank, solution in enumerate(ranked, 1):
            annotation = ax.annotate(
                str(rank), (solution.cut_size, solution.area_imbalance),
                xytext=offsets[0], textcoords="offset points", ha="center", va="center",
                fontsize=14, fontweight="bold", color="black", zorder=5,
                path_effects=[path_effects.withStroke(linewidth=3, foreground="white")],
            )
            for offset in offsets:
                annotation.set_position(offset)
                box = annotation.get_window_extent(renderer).expanded(1.4, 1.3)
                if not any(box.overlaps(other) for other in label_boxes):
                    break
            label_boxes.append(box)
    return fig


def make_merged_plot(solutions_by_metric: dict[str, list[Solution]], top_n: int = 5):
    """Draw WNS/TNS above rWL/Power, with shared outer titles and no colorbars."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with plt.rc_context(PLOT_STYLE):
        fig, axes = plt.subplots(
            2, 2, figsize=(14, 9.5), sharex=True, sharey=True, layout="none",
            gridspec_kw={"wspace": 0, "hspace": 0},
        )
        fig.subplots_adjust(left=0.075, right=0.99, bottom=0.085, top=0.97,
                            wspace=0, hspace=0)
        fig.supxlabel("Cut Size", y=0.015, fontsize=20, fontweight="bold")
        fig.supylabel("Area Imbalance", x=0.012, fontsize=20, fontweight="bold")
        try:
            for ax, metric in zip(axes.flat, MERGE_METRICS):
                make_plot(
                    solutions_by_metric[metric], top_n, metric, ax=ax,
                    show_colorbar=False, show_axis_titles=False,
                )
                ax.label_outer()
                # One shared scientific multiplier avoids text along the panel seam.
                ax.yaxis.get_offset_text().set_visible(ax is axes[0, 0])
        except Exception:
            plt.close(fig)
            raise
    return fig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plot partition cut size vs. area imbalance, or merge all four PPA plots into a 2x2 figure.",
        epilog="Numbers rank the selected metric closest to zero (1 = closest); missing values are omitted. Requires matplotlib.",
    )
    parser.add_argument("solutions_dir", type=Path, help="Directory containing sNNNN/solutions")
    parser.add_argument("ppa_csv", type=Path, help="CSV from summarize_partitions.py (idx and selected metric required)")
    parser.add_argument("--metric", type=str.lower, choices=(*METRICS, "merge"), default="wns",
                        help="Metric for colors and ranks, or merge for a 2x2 figure (default: wns)")
    parser.add_argument("-o", "--output", type=Path,
                        help="PNG, PDF, or SVG output (default: beside CSV, partition_METRIC.png)")
    parser.add_argument("--top-n", type=int, default=5, help="Number of ranks to label (default: 5)")
    args = parser.parse_args(argv)
    solutions_dir = args.solutions_dir.expanduser().resolve()
    csv_path = args.ppa_csv.expanduser().resolve()
    output = args.output.expanduser() if args.output else csv_path.with_name(f"partition_{args.metric}.png")
    if not solutions_dir.is_dir():
        parser.error(f"solutions directory does not exist: {solutions_dir}")
    if not csv_path.is_file():
        parser.error(f"PPA CSV does not exist: {csv_path}")
    if output.suffix.lower() not in (".png", ".pdf", ".svg"):
        parser.error("output filename must end in .png, .pdf, or .svg")
    if args.top_n < 0:
        parser.error("--top-n must be nonnegative")
    try:
        metrics = MERGE_METRICS if args.metric == "merge" else (args.metric,)
        solutions_by_metric = {
            metric: load_solutions(solutions_dir, csv_path, metric) for metric in metrics
        }
        if args.metric == "merge":
            fig = make_merged_plot(solutions_by_metric, args.top_n)
        else:
            fig = make_plot(solutions_by_metric[args.metric], args.top_n, args.metric)
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(output, dpi=300)
        finally:
            import matplotlib.pyplot as plt
            plt.close(fig)
    except ModuleNotFoundError as error:
        parser.error(f"missing dependency {error.name}; install matplotlib with: python3 -m pip install matplotlib")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.metric == "merge":
        print(f"Saved {output.resolve()} (2x2: WNS, TNS, rWL, Power)")
    for metric, solutions in solutions_by_metric.items():
        spec = METRICS[metric]
        missing = sum(solution.value is None for solution in solutions)
        prefix = spec.label if args.metric == "merge" else f"Saved {output.resolve()}"
        print(f"{prefix} ({len(solutions) - missing} plotted, {missing} without {spec.label} omitted)")
        balanced = balanced_min_cut(solutions)
        if balanced is not None:
            visibility = "" if balanced.value is not None else f" (not shown: {spec.label} unavailable)"
            print(f"Balanced Min-Cut: idx={balanced.idx}, cut_size={balanced.cut_size:g}, "
                  f"area_imbalance={balanced.area_imbalance:.2f}{visibility}")
        for rank, solution in enumerate(top_solutions(solutions, args.top_n), 1):
            print(f"Rank {rank}: idx={solution.idx}, {spec.label}={solution.value:g} {spec.unit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
