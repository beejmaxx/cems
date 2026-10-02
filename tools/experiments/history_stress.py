"""Independent inverse-rank oracle for generated tests."""
from fractions import Fraction as Q
from pathlib import Path
from swg import probability

class InverseOracle:
    """TEST-only trusted-file reader plus inverse rank, not forward enumeration.

    Direct rational evaluation of the ORIGINAL model selects a score band.
    Bottom-up suffix counts then derive its rank independently of C++ replay.
    This keeps complexity tied to model structure, not covered candidate count.
    """
    def __init__(self, path, graphs, weights):
        raw = Path(path).read_bytes()
        assert raw[:8] == b"PLAB0002"
        position = 8

        def take(n):
            nonlocal position
            value = int.from_bytes(raw[position:position+n], "little")
            position += n
            return value

        self.denominator = take(16)
        nn, nb, complete, next_score = take(4), take(4), take(1), take(16)
        self.nodes, counts = [], []
        for _ in range(nn):
            terminal, size = take(1), take(2)
            edges, before = {}, terminal
            for _ in range(size):
                byte, child = take(1), take(4)
                edges[byte] = (child, before)
                before += counts[child]
            self.nodes.append((terminal, edges))
            counts.append(before)
        self.bands, offset = {}, 0
        for _ in range(nb):
            score, root = take(16), take(4)
            self.bands[score] = (root, offset)
            offset += counts[root]
        assert position == len(raw)
        self.count = offset
        self.graphs = graphs
        total = sum(map(Q, weights.values()))
        self.weights = {n: Q(w) / total for n, w in weights.items()}
        self.complete, self.next_score = bool(complete), Q(next_score, self.denominator)

    def score(self, value):
        return sum(self.weights[n] * probability(g, value) for n, g in self.graphs.items())

    def rank(self, value):
        score = self.score(value) * self.denominator
        if score.denominator != 1 or score.numerator not in self.bands:
            return None
        node, rank = self.bands[score.numerator]
        for byte in value:
            edge = self.nodes[node][1].get(byte)
            if edge is None:
                return None
            node, before = edge
            rank += before
        return rank if self.nodes[node][0] else None
