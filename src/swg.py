"""Exact weighted-graph serialization and evaluation for offline preparation."""
from fractions import Fraction as Q
import hashlib
import math
from pathlib import Path

MAX = (1 << 128) - 1


def graph_integer_form(graph):
    """Exact rational-to-integer change of units; preserve frozen graph semantics."""
    nodes = graph["nodes"]
    units, converted, active = {}, {}, set()

    def visit(i):
        if i in active:
            raise ValueError("cyclic fixture")
        if i in units:
            return units[i]
        active.add(i)
        node = nodes[i]
        factors = [Q(p) * visit(child) for _, child, p in node["edges"]]
        values = ([Q(node["final"])] if Q(node["final"]) else []) + factors
        if not values or any(v <= 0 for v in values):
            raise ValueError("invalid fixture mass")
        unit = Q(math.gcd(*(v.numerator for v in values)), math.lcm(*(v.denominator for v in values)))

        def exact(value):
            value = Q(value)
            if value.denominator != 1 or not 0 <= value <= MAX:
                raise ValueError("fixture integer limit")
            return int(value)

        converted[i] = (exact(Q(node["final"]) / unit),
                        [(c, child, exact(f / unit)) for (c, child, _), f in zip(node["edges"], factors)])
        units[i] = unit
        active.remove(i)
        return unit

    root = visit(0)
    for i in range(len(nodes)):
        visit(i)
    return Q(graph["initial"]) * root, [converted[i] for i in range(len(nodes))]


def write_source(path, graphs, weights):
    """Serialize SWG1 inputs; compilation itself is native."""
    if set(graphs) != set(weights) or any(Q(w) <= 0 for w in weights.values()):
        raise ValueError("positive exact branch weights required")
    total = sum(map(Q, weights.values()))
    converted = [(name, *graph_integer_form(g)) for name, g in sorted(graphs.items())]
    factors = [Q(weights[name]) / total * initial for name, initial, _ in converted]
    denominator = math.lcm(*(q.denominator for q in factors))
    if denominator > MAX:
        raise ValueError("fixture common denominator exceeds uint128")
    count = sum(len(nodes) for _, _, nodes in converted)
    lines = [f"SWG1 {denominator} {count} {len(converted)}"]
    offset = 0
    for factor, (_, _, nodes) in zip(factors, converted):
        lines.append(f"{offset} {int(factor * denominator)}")
        offset += len(nodes)
    offset = 0
    for _, _, nodes in converted:
        for final, edges in nodes:
            row = [final, len(edges)]
            for c, child, factor in edges:
                row += [c, child + offset, factor]
            lines.append(" ".join(map(str, row)))
        offset += len(nodes)
    raw = ("\n".join(lines) + "\n").encode()
    if len(raw) > 64 * 1024**2:
        raise ValueError("fixture file cap")
    with Path(path).open("xb") as f:
        f.write(raw)
    Path(path).chmod(0o600)
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "nodes": count, "branches": len(converted), "integer_bits": denominator.bit_length()}


def probability(graph, value):
    """Direct original rational edge evaluation, independent of the C++ compiler."""
    p, i = Q(graph["initial"]), 0
    for c in value:
        edge = next((e for e in graph["nodes"][i]["edges"] if e[0] == c), None)
        if edge is None:
            return Q(0)
        _, i, w = edge
        p *= Q(w)
    return p * Q(graph["nodes"][i]["final"])
