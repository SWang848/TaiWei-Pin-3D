# Place-MoL partition port

These files were ported from the user-supplied
`Open3DBench_2/Open3DBench/Place-MoL/src/partition` implementation:

| Local file | Reference file |
| --- | --- |
| [graph_builder.py](graph_builder.py) | `common/graph_builder.py` |
| [graph_cut.py](graph_cut.py) | `graph_cut/graph_cut.py` |
| [gin_network.py](gin_network.py) | `common/gin_network.py` |
| [GNN_partition.py](GNN_partition.py) | `GNN/GNN_partition.py` |

Algorithm bodies are retained. Integration-only changes are:

- Lazy PyTorch/PyG imports in the graph builder, so min/max cut need only NumPy.
- Empty edge arrays have shape `(0, 2)` so edgeless graphs remain valid PyG inputs.
- A local relative GIN import; removal of unused debugger imports and the
  graph-cut module's global `sys.path` modification.
- [opendb_adapter.py](opendb_adapter.py) provides the PlaceDB attributes used by
  the original graph builder from TaiWei's geometry/pin snapshot.

The adapter uses TaiWei's BLOCK master classification and physical dimensions
in microns. Nonmacro instances and zero-area top-level IO nodes are merged as
cells by the reference builder. Signal and clock nets are supplied without
fanout filtering. The cells' average aspect ratio, total pin count and distinct
connected-cell counts feed the original five-dimensional node features.

Reference quirks are preserved, including macro-only cut evaluation, the exact
pair-swap gain formula, deterministic descending-area initialization, and GNN
area refinement that can exhaust its iterations outside the tolerance. Do not
interpret the retained name `min_cut` as a certified global minimum or
`max_cut` as a certified global maximum. Exported metrics always describe the
actual final assignment under the reference evaluator.

The runner [generate_placemol_macro_partitions.py](../../generate_placemol_macro_partitions.py)
seeds Python, NumPy and PyTorch for each run. The original KMeans seed remains
42. Each method/run gets a solution even when assignments coincide; there is
no Pareto filtering. TaiWei's existing exporter handles exact macro names and
physical tier-label conversion.
