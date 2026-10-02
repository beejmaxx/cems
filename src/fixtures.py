"""Small explicit graphs for generated controls and synthetic rehearsals.

The exporter aliases preserve existing qualification and archived source imports.
Production graph serialization and evaluation live in swg.py.
"""
from fractions import Fraction as Q
from swg import graph_integer_form, probability, write_source


def explicit_graph(masses):
    """Trie for small independently enumerated tests, not a production model."""
    nodes = [{"final": 0, "edges": {}}]
    for value, mass in sorted(masses.items()):
        if type(value) is not bytes or not isinstance(mass, int) or mass <= 0:
            raise ValueError("invalid test mass")
        i = 0
        for c in value:
            if c not in nodes[i]["edges"]:
                nodes[i]["edges"][c] = len(nodes)
                nodes.append({"final": 0, "edges": {}})
            i = nodes[i]["edges"][c]
        nodes[i]["final"] = mass
    return {"initial": str(Q(1, sum(masses.values()))), "nodes": [
        {"final": str(n["final"]), "edges": [[c, child, "1"] for c, child in sorted(n["edges"].items())]}
        for n in nodes]}
