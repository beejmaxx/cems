"""Generated decimal-language fixtures; no external model inputs."""
from fractions import Fraction as Q

def decimal_graph(length=14, reversed_weights=False):
    weights = list(range(10, 0, -1)) if reversed_weights else [1] * 10
    nodes = [{"final": "0", "edges": [[48 + c, i + 1, str(w)] for c, w in enumerate(weights)]}
             for i in range(length)]
    nodes.append({"final": "1", "edges": []})
    return {"initial": str(Q(1, sum(weights) ** length)), "nodes": nodes}
