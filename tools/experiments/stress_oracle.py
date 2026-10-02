"""Bounded TEST oracle: prefix-heap traversal, independent of native score bands."""
from fractions import Fraction as Q
import heapq
import time

from swg import probability


class HeapIndex:
    """Enumerate one deterministic graph with maximum-completion bounds.

    This is intentionally NOT a production generator. It keeps a bounded heap
    and a small explicit result cache solely to check the native implementation.
    """
    def __init__(self, graph):
        self.graph = graph
        self.maximum, self.counts, self.mass = {}, {}, {}
        self._visit(0)
        self.count = self.counts[0]
        self.total_mass = Q(graph["initial"]) * self.mass[0]
        self.heap = [(-Q(graph["initial"]) * self.maximum[0], b"", False, 0, Q(graph["initial"]))]
        self.cache = []

    def _visit(self, node):
        if node in self.maximum:
            return
        g = self.graph["nodes"][node]
        best, mass, count = Q(g["final"]), Q(g["final"]), int(bool(Q(g["final"])))
        for _, child, weight in g["edges"]:
            self._visit(child)
            best = max(best, Q(weight) * self.maximum[child])
            mass += Q(weight) * self.mass[child]
            count += self.counts[child]
        self.maximum[node], self.mass[node], self.counts[node] = best, mass, count

    def at(self, rank):
        if not 0 <= rank < min(self.count, 100_000):
            raise ValueError("test oracle coordinate bound")
        began = time.monotonic()
        while len(self.cache) <= rank:
            if len(self.heap) > 100_000 or time.monotonic() - began > 10:
                raise RuntimeError("test prefix-heap oracle limit")
            negative_bound, prefix, leaf, node, factor = heapq.heappop(self.heap)
            if leaf:
                self.cache.append((prefix, -negative_bound))
                continue
            g = self.graph["nodes"][node]
            if Q(g["final"]):
                heapq.heappush(self.heap, (-factor * Q(g["final"]), prefix, True, node, factor))
            for byte, child, weight in g["edges"]:
                amplitude = factor * Q(weight)
                heapq.heappush(self.heap, (-amplitude * self.maximum[child], prefix + bytes([byte]), False, child, amplitude))
        return self.cache[rank]

    def probability(self, value):
        return probability(self.graph, value)


class Snapshot:
    def __init__(self, graph):
        self.index = HeapIndex(graph)
        self.count = self.index.count


def prefixed(graph, prefix):
    """Injective byte transformation; exact branch mass and dependencies retained."""
    size = len(prefix)
    nodes = [{"final": "0", "edges": [[byte, i + 1, "1"]]} for i, byte in enumerate(prefix)]
    nodes += [{"final": node["final"], "edges": [[byte, child + size, weight] for byte, child, weight in node["edges"]]}
              for node in graph["nodes"]]
    return {"initial": graph["initial"], "nodes": nodes}


def swapped_case(graph):
    def swap(byte):
        return byte ^ 32 if 65 <= byte <= 90 or 97 <= byte <= 122 else byte
    return {"initial": graph["initial"], "nodes": [
        {"final": node["final"], "edges": sorted([[swap(byte), child, weight] for byte, child, weight in node["edges"]])}
        for node in graph["nodes"]]}


def wrapped_prefix_free(graph, choice):
    """Bind one finite choice, reuse it at both ends. No independent resampling.

    Each choice is a branch decision, weighted ONCE by the surrounding mixture.
    This helper requires prefix-free inputs rather than hiding ambiguous joins.
    """
    if any(Q(node["final"]) and node["edges"] for node in graph["nodes"]):
        raise ValueError("test wrapper requires prefix-free graph")
    if not choice:
        return prefixed(graph, b"")
    nodes = [{"final": node["final"], "edges": [list(e) for e in node["edges"]]} for node in graph["nodes"]]
    start = len(nodes)
    nodes += [{"final": "0", "edges": [[byte, start + i + 1, "1"]]} for i, byte in enumerate(choice)]
    nodes.append({"final": "1", "edges": []})
    for node in nodes[:start]:
        if Q(node["final"]):
            node["edges"] = [[choice[0], start + 1, node["final"]]]
            node["final"] = "0"
    return prefixed({"initial": graph["initial"], "nodes": nodes}, choice)
