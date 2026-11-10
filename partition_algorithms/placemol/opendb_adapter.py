"""Translate a TaiWei OpenDB snapshot into the PlaceDB fields Place-MoL uses.

Dimensions are in microns. BLOCK instances are macros; other instances and
zero-area IO terminals are cells. Pin multiplicity is retained here so the
reference GraphBuilder itself decides how to collapse each net.
"""

import csv
from dataclasses import dataclass
import math
from pathlib import Path
from types import SimpleNamespace

import generate_macro_partitions as exporter


@dataclass(frozen=True)
class Instance:
    name: str
    master: str
    kind: str
    width: float
    height: float


def read_snapshot(path):
    """Validate dimensions and pin references before constructing a graph."""
    instances, nets, names = [], [], set()
    with Path(path).open(encoding="utf-8", newline="") as handle:
        if handle.readline().rstrip("\r\n") != "# taiwei-placemol-graph-v1":
            raise ValueError(f"{path}: unsupported Place-MoL snapshot format")
        for line_no, row in enumerate(csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE), 2):
            try:
                if len(row) == 7 and row[0] == "node" and not nets:
                    _, raw_id, raw_name, master, kind, raw_width, raw_height = row
                    name = exporter.normalize_name(raw_name)
                    width, height = float(raw_width), float(raw_height)
                    if (int(raw_id) != len(instances) or not name or name in names
                            or any(c.isspace() for c in name) or not master
                            or kind not in ("macro", "cell", "port")
                            or not all(math.isfinite(x) and x >= 0 for x in (width, height))
                            or (kind != "port" and (width <= 0 or height <= 0))
                            or (kind == "port" and (width != 0 or height != 0))):
                        raise ValueError("invalid or duplicate instance")
                    instances.append(Instance(name, master, kind, width, height))
                    names.add(name)
                elif len(row) >= 2 and row[0] == "net":
                    pins = [int(value) for value in row[2:]]
                    if int(row[1]) != len(pins) or any(i < 0 or i >= len(instances) for i in pins):
                        raise ValueError("invalid net pins")
                    nets.append(pins)
                else:
                    raise ValueError("invalid snapshot row")
            except ValueError as error:
                raise ValueError(f"{path}:{line_no}: {error}") from error
    if not any(node.kind == "macro" for node in instances):
        raise ValueError(f"{path}: no BLOCK macros found in the 2D seed")
    return instances, nets


def make_problem_instance(instances, nets, *, scale_factor=1.0, top_k_ratio=0.3, k_hop=1):
    """Provide the reference's narrow PlaceDB interface without DreamPlace."""
    node2pin = [[] for _ in instances]
    net2pin, pin2node, pin2net = [], [], []
    for net_id, nodes in enumerate(nets):
        pins = []
        for node_id in nodes:
            pin_id = len(pin2node)
            pins.append(pin_id)
            node2pin[node_id].append(pin_id)
            pin2node.append(node_id)
            pin2net.append(net_id)
        net2pin.append(pins)
    placedb = SimpleNamespace(
        node_names=[node.name for node in instances],
        node_name2id_map={node.name: i for i, node in enumerate(instances)},
        node_size_x=[node.width for node in instances],
        node_size_y=[node.height for node in instances],
        node2pin_map=node2pin, net2pin_map=net2pin,
        pin2node_map=pin2node, pin2net_map=pin2net,
    )
    return SimpleNamespace(
        dmp_placedb=placedb,
        macros=[i for i, node in enumerate(instances) if node.kind == "macro"],
        args=SimpleNamespace(partition_params={
            "scale_factor": scale_factor, "top_k_ratio": top_k_ratio, "k_hop": k_hop,
        }),
    )
