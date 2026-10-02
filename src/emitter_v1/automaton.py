"""Bounded acyclic rational-weight compiler and unique-output index.

This backend sums latent explanation probabilities before ranking strings. It
does not use a seen-password table. Weighted state subsets and suffix probability
histograms can grow exponentially: hard limits reject unsupported expansion.
The existing raw indexed backend remains available, with different guarantees.
"""
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from fractions import Fraction as Q
import itertools

from .model import Unsupported, LimitExceeded, ascii_bytes, rational, apply_transform, identity, literal, transform


@dataclass(frozen=True)
class Limits:
    nfa_states: int = 20_000
    dfa_states: int = 5_000
    transitions: int = 200_000
    probability_entries: int = 50_000
    finite_expansion: int = 4096
    fraction_bits: int = 4096
    max_length: int = 128


def weights(expr):
    if expr.get("mode") != "probability":
        raise Unsupported("ordinal costs are not probabilities")
    options = expr["options"]
    if not options or len({x["id"] for x in options}) != len(options):
        raise ValueError("empty choice or duplicate explanation id")
    values = [rational(x["weight"]) for x in options]
    total = sum(values)
    return [(x["expr"], v / total) for x, v in zip(options, values)]


def validate(expr, bound=frozenset()):
    """Validate all admitted branches, including unused latent bindings."""
    op = expr["op"]
    if op == "literal":
        ascii_bytes(expr["value"])
    elif op == "words":
        alphabet, length = ascii_bytes(expr["alphabet"]), expr["length"]
        if not alphabet or len(set(alphabet)) != len(alphabet) or type(length) is not int or not 1 <= length <= 128:
            raise Unsupported("invalid bounded word domain")
    elif op == "ref":
        if expr["name"] not in bound:
            raise Unsupported("unbound or forward reference")
    elif op == "choice":
        for child, _ in weights(expr):
            validate(child, bound)
    elif op == "concat":
        for child in expr["parts"]:
            validate(child, bound)
    elif op == "transform":
        validate(expr["expr"], bound)
        if isinstance(expr["operation"], dict):
            if expr["operation"]["op"] != "ref":
                raise Unsupported("dynamic operation must reference a binding")
            validate(expr["operation"], bound)
        else:
            apply_transform(b"", expr["operation"])
    elif op == "conditional":
        if expr["selector"] not in bound or not expr["cases"]:
            raise Unsupported("invalid conditional selector/cases")
        for value, child in expr["cases"].items():
            ascii_bytes(value)
            validate(child, bound)
    elif op == "let":
        if expr.get("base_cost", 0) != 0:
            raise Unsupported("ordinal base costs are not probability weights")
        local = set(bound)
        for binding in expr["bindings"]:
            if binding["name"] in local:
                raise Unsupported("duplicate/shadowed binding")
            validate(binding["expr"], local)
            local.add(binding["name"])
        validate(expr["output"], local)
    else:
        raise Unsupported("unknown emitter operator: " + str(op))


def references(expr):
    counts, selectors = Counter(), set()
    op = expr["op"]
    if op == "ref":
        counts[expr["name"]] += 1
    elif op == "conditional":
        counts[expr["selector"]] += 1
        selectors.add(expr["selector"])
        for child in expr["cases"].values():
            c, s = references(child)
            counts.update(c)
            selectors.update(s)
    else:
        children = []
        if op == "concat":
            children = expr["parts"]
        elif op == "choice":
            children = [x["expr"] for x in expr["options"]]
        elif op == "transform":
            children = [expr["expr"]]
            if isinstance(expr["operation"], dict):
                children.append(expr["operation"])
                selectors.add(expr["operation"]["name"])
        elif op == "let":
            children = [b["expr"] for b in expr["bindings"]] + [expr["output"]]
        for child in children:
            c, s = references(child)
            counts.update(c)
            selectors.update(s)
        if op == "let":
            for binding in expr["bindings"]:
                counts.pop(binding["name"], None)
                selectors.discard(binding["name"])
    return counts, selectors


def resolve(expr, env, deferred=frozenset()):
    op = expr["op"]
    if op == "ref":
        if expr["name"] in deferred:
            return expr
        if expr["name"] not in env:
            raise Unsupported("unbound or forward reference: " + expr["name"])
        return env[expr["name"]]
    if op == "conditional":
        if expr["selector"] in deferred:
            return dict(expr, cases={k: resolve(v, env, deferred) for k, v in expr["cases"].items()})
        selected = env.get(expr["selector"])
        if not selected or selected["op"] != "literal":
            raise Unsupported("conditional selector requires a bounded concrete binding")
        if selected["value"] not in expr["cases"]:
            raise Unsupported("conditional lacks a case for an admitted selector")
        return resolve(expr["cases"][selected["value"]], env, deferred)
    if op == "concat":
        return dict(expr, parts=[resolve(x, env, deferred) for x in expr["parts"]])
    if op == "choice":
        return dict(expr, options=[dict(x, expr=resolve(x["expr"], env, deferred)) for x in expr["options"]])
    if op == "transform":
        operation = expr["operation"]
        if isinstance(operation, dict):
            if operation["name"] in deferred:
                return dict(expr, expr=resolve(expr["expr"], env, deferred))
            selected = resolve(operation, env, deferred)
            if selected["op"] != "literal":
                raise Unsupported("dynamic transform requires a concrete operation")
            operation = selected["value"]
        return transform(resolve(expr["expr"], env, deferred), operation)
    if op == "let":
        # Local names are masked; nested programs resolve their own bindings.
        local_names = {b["name"] for b in expr["bindings"]}
        if local_names & (set(env) | set(deferred)):
            raise Unsupported("nested binding shadowing is not supported")
        local = set(deferred)
        bindings = []
        for binding in expr["bindings"]:
            bindings.append(dict(binding, expr=resolve(binding["expr"], env, local)))
            local.add(binding["name"])
        # Keep local references, while substituting enclosing context.
        return dict(expr, bindings=bindings, output=resolve(expr["output"], env, local))
    if op in {"literal", "words"}:
        return expr
    raise Unsupported("unknown emitter operator: " + str(op))


def finite_distribution(expr, limit=4096):
    """Bounded exact expansion for small shared/context decisions only."""
    op = expr["op"]
    if op == "literal":
        return {ascii_bytes(expr["value"]): Q(1)}
    if op == "words":
        alphabet = ascii_bytes(expr["alphabet"])
        n = len(alphabet) ** expr["length"]
        if not alphabet or len(set(alphabet)) != len(alphabet) or n > limit:
            raise LimitExceeded("shared/context word domain exceeds finite expansion limit")
        return {bytes(v): Q(1, n) for v in itertools.product(alphabet, repeat=expr["length"])}
    if op == "choice":
        result = defaultdict(Q)
        visited = 0
        for child, weight in weights(expr):
            values = finite_distribution(child, limit)
            visited += len(values)
            if visited > limit:
                raise LimitExceeded("finite choice expansion limit")
            for value, probability in values.items():
                result[value] += weight * probability
        return dict(result)
    if op == "concat":
        result = {b"": Q(1)}
        for child in expr["parts"]:
            values = finite_distribution(child, limit)
            if len(result) * len(values) > limit:
                raise LimitExceeded("finite product expansion limit")
            joined = defaultdict(Q)
            for a, p in result.items():
                for b, q in values.items():
                    joined[a + b] += p * q
            result = dict(joined)
        return result
    if op == "transform":
        result = defaultdict(Q)
        for value, probability in finite_distribution(expr["expr"], limit).items():
            result[apply_transform(value, expr["operation"])] += probability
        return dict(result)
    if op == "let":
        result = defaultdict(Q)
        for env, probability in contexts(expr, limit):
            for value, p in finite_distribution(resolve(expr["output"], env), limit).items():
                result[value] += probability * p
                if len(result) > limit:
                    raise LimitExceeded("finite program expansion limit")
        return dict(result)
    raise Unsupported("unresolved finite expression")


def contexts(program, limit):
    if program.get("base_cost", 0) != 0:
        raise Unsupported("probabilistic program cannot inherit an ordinal base cost")
    bindings = program["bindings"]
    names = [b["name"] for b in bindings]
    if len(names) != len(set(names)):
        raise ValueError("duplicate binding")
    states = [({}, Q(1))]
    for i, binding in enumerate(bindings):
        count, selectors = references(program["output"])
        later = set()
        for following in bindings[i + 1:]:
            c, s = references(following["expr"])
            count.update(c)
            selectors.update(s)
            later.update(c)
        name = binding["name"]
        concrete = count[name] > 1 or name in selectors or name in later
        expanded = []
        for env, probability in states:
            expr = resolve(binding["expr"], env)
            if concrete:
                values = finite_distribution(expr, limit)
                if len(expanded) + len(values) > limit:
                    raise LimitExceeded("shared/context state expansion exceeds limit")
                for value, p in values.items():
                    expanded.append((dict(env, **{name: literal(value.decode("ascii"))}), probability * p))
            else:
                expanded.append((dict(env, **{name: expr}), probability))
        states = expanded
    return states


class NFA:
    def __init__(self, limits):
        self.limits, self.edges = limits, []
        self.edge_count = 0
        self.start, self.final = self.state(), self.state()

    def state(self):
        if len(self.edges) >= self.limits.nfa_states:
            raise LimitExceeded("NFA state limit")
        self.edges.append([])
        return len(self.edges) - 1

    def edge(self, source, label, dest, weight=Q(1)):
        self.edge_count += 1
        if self.edge_count > self.limits.transitions:
            raise LimitExceeded("NFA transition limit")
        self.edges[source].append((label, dest, weight))

    def emit(self, expr, start, end):
        op = expr["op"]
        if op == "literal":
            data = ascii_bytes(expr["value"])
            if len(data) > self.limits.max_length:
                raise LimitExceeded("literal length limit")
            current = start
            for i, byte in enumerate(data):
                following = end if i == len(data) - 1 else self.state()
                self.edge(current, byte, following)
                current = following
            if not data:
                self.edge(start, None, end)
        elif op == "words":
            self.emit_words(expr, start, end, "identity")
        elif op == "concat":
            current = start
            for i, part in enumerate(expr["parts"]):
                following = end if i == len(expr["parts"]) - 1 else self.state()
                self.emit(part, current, following)
                current = following
            if not expr["parts"]:
                self.edge(start, None, end)
        elif op == "choice":
            for child, probability in weights(expr):
                a, b = self.state(), self.state()
                self.edge(start, None, a, probability)
                self.emit(child, a, b)
                self.edge(b, None, end)
        elif op == "let":
            for env, probability in contexts(expr, self.limits.finite_expansion):
                a, b = self.state(), self.state()
                self.edge(start, None, a, probability)
                self.emit(resolve(expr["output"], env), a, b)
                self.edge(b, None, end)
        elif op == "transform":
            self.emit_transformed(expr["expr"], expr["operation"], start, end)
        else:
            raise Unsupported("unresolved or unsupported emitter: " + op)

    def emit_words(self, expr, start, end, operation):
        alphabet, length = ascii_bytes(expr["alphabet"]), expr["length"]
        if not alphabet or len(set(alphabet)) != len(alphabet) or type(length) is not int or not 1 <= length <= self.limits.max_length:
            raise Unsupported("invalid word domain")
        current = start
        for i in range(length):
            following = end if i == length - 1 else self.state()
            for byte in alphabet:
                kind = ("upper" if i == 0 else "lower") if operation == "title" else operation
                mapped = apply_transform(bytes([byte]), kind)
                self.edge(current, mapped[0], following, Q(1, len(alphabet)))
            current = following

    def emit_transformed(self, expr, operation, start, end):
        if not isinstance(operation, str):
            raise Unsupported("unresolved dynamic transform")
        apply_transform(b"", operation)  # Validate even an empty expression.
        op = expr["op"]
        if operation == "identity":
            self.emit(expr, start, end)
        elif op == "literal":
            self.emit(literal(apply_transform(ascii_bytes(expr["value"]), operation).decode()), start, end)
        elif op == "words":
            self.emit_words(expr, start, end, operation)
        elif op == "choice":
            self.emit(dict(expr, options=[dict(x, expr=transform(x["expr"], operation)) for x in expr["options"]]), start, end)
        elif op == "concat" and operation in {"lower", "upper", "reverse"}:
            parts = expr["parts"][::-1] if operation == "reverse" else expr["parts"]
            self.emit(dict(expr, parts=[transform(x, operation) for x in parts]), start, end)
        elif op == "let":
            self.emit(dict(expr, output=transform(expr["output"], operation)), start, end)
        else:
            # Nonlocal transforms of complicated bounded expressions may expand.
            values = finite_distribution(transform(expr, operation), self.limits.finite_expansion)
            for value, probability in values.items():
                a = self.state()
                self.edge(start, None, a, probability)
                self.emit(literal(value.decode()), a, end)

    def topological(self):
        incoming = [0] * len(self.edges)
        for edges in self.edges:
            for _, dest, _ in edges:
                incoming[dest] += 1
        queue = deque(i for i, n in enumerate(incoming) if not n)
        order = []
        while queue:
            state = queue.popleft()
            order.append(state)
            for _, dest, _ in self.edges[state]:
                incoming[dest] -= 1
                if not incoming[dest]:
                    queue.append(dest)
        if len(order) != len(self.edges):
            raise Unsupported("cyclic construction is outside this backend")
        return order

    def determinize(self):
        order = self.topological()

        def closure(seed):
            values = defaultdict(Q, seed)
            for state in order:
                if values.get(state):
                    for label, dest, probability in self.edges[state]:
                        if label is None:
                            values[dest] += values[state] * probability
            scale = sum(values.values())
            key = tuple((s, p / scale) for s, p in sorted(values.items()) if p)
            for _, p in key:
                if max(p.numerator.bit_length(), p.denominator.bit_length()) > self.limits.fraction_bits:
                    raise LimitExceeded("residual rational weight complexity limit")
            return key, scale

        first, initial = closure({self.start: Q(1)})
        keys, index, nodes = [first], {first: 0}, []
        transitions = 0
        for key in keys:
            grouped = {}
            for state, probability in key:
                for label, dest, weight in self.edges[state]:
                    if label is not None:
                        grouped.setdefault(label, defaultdict(Q))[dest] += probability * weight
            edges = []
            for label, seed in sorted(grouped.items()):
                following, scale = closure(seed)
                if following not in index:
                    if len(keys) >= self.limits.dfa_states:
                        raise LimitExceeded("weighted deterministic state limit")
                    index[following] = len(keys)
                    keys.append(following)
                edges.append((label, index[following], scale))
                transitions += 1
                if transitions > self.limits.transitions:
                    raise LimitExceeded("deterministic transition limit")
            nodes.append({"final": dict(key).get(self.final, Q(0)), "edges": edges})
        return UniqueIndex(nodes, initial, self.limits, {"nfa_states": len(self.edges),
            "nfa_transitions": self.edge_count, "dfa_states": len(nodes), "dfa_transitions": transitions})


class UniqueIndex:
    """Exact descending TOTAL string probability; ties use byte-lexicographic order.

    Per-state suffix-probability histograms enable rank/unrank. Their bounded size
    is a separate gate from DFA size; not every compact automaton has few scores.
    """
    def __init__(self, nodes, initial, limits=Limits(), stats=None):
        self.nodes, self.initial, self.limits = nodes, initial, limits
        self.stats, self.levels = dict(stats or {}), {}
        self.longest = {}
        self._visiting = set()
        self._entries = 0
        self._build(0, 0)
        self.bands = sorted(self.levels[0].items(), reverse=True)
        # Frozen rank semantics are unchanged. Inverse lookup need not rescan
        # every probability band for every candidate/history membership query.
        self.band_offsets = {}
        offset = 0
        for probability, count in self.bands:
            self.band_offsets[probability] = offset
            offset += count
        self.count = sum(self.levels[0].values())
        self.total_mass = initial * sum(p * n for p, n in self.bands)
        if self.total_mass != 1:
            raise ValueError("probability mass must sum to exactly one")
        self.stats["probability_entries"] = self._entries

    def _build(self, node, depth):
        if node in self._visiting:
            raise Unsupported("cyclic index")
        if depth > self.limits.max_length:
            raise LimitExceeded("output length limit")
        if node in self.levels:
            if depth + self.longest[node] > self.limits.max_length:
                raise LimitExceeded("output length limit")
            return self.levels[node]
        self._visiting.add(node)
        data = self.nodes[node]
        levels = defaultdict(int)
        if data["final"]:
            levels[data["final"]] += 1
        for _, dest, weight in data["edges"]:
            for probability, count in self._build(dest, depth + 1).items():
                p = probability * weight
                if max(p.numerator.bit_length(), p.denominator.bit_length()) > self.limits.fraction_bits:
                    raise LimitExceeded("suffix probability rational complexity limit")
                levels[p] += count
                if self._entries + len(levels) > self.limits.probability_entries:
                    raise LimitExceeded("suffix probability histogram limit")
        self._entries += len(levels)
        if self._entries > self.limits.probability_entries:
            raise LimitExceeded("suffix probability histogram limit")
        self._visiting.remove(node)
        self.levels[node] = dict(levels)
        self.longest[node] = max([0] + [1 + self.longest[d] for _, d, _ in data["edges"]])
        if depth + self.longest[node] > self.limits.max_length:
            raise LimitExceeded("output length limit")
        return self.levels[node]

    def probability(self, value):
        node, probability = 0, self.initial
        for byte in value:
            edge = next((x for x in self.nodes[node]["edges"] if x[0] == byte), None)
            if edge is None:
                return Q(0)
            _, node, factor = edge
            probability *= factor
        return probability * self.nodes[node]["final"]

    def at(self, rank):
        if not 0 <= rank < self.count:
            raise ValueError("unique rank outside snapshot")
        for p, count in self.bands:
            if rank < count:
                break
            rank -= count
        node, output, overall = 0, bytearray(), p * self.initial
        while True:
            data = self.nodes[node]
            if data["final"] == p:
                if rank == 0:
                    return bytes(output), overall
                rank -= 1
            for byte, dest, factor in data["edges"]:
                count = self.levels[dest].get(p / factor, 0)
                if rank < count:
                    output.append(byte)
                    node, p = dest, p / factor
                    break
                rank -= count
            else:
                raise AssertionError("invalid probability-band rank")

    def rank(self, value):
        p = self.probability(value) / self.initial
        if not p:
            return None
        result = self.band_offsets[p]
        node = 0
        for byte in value:
            data = self.nodes[node]
            if data["final"] == p:
                result += 1
            for label, dest, factor in data["edges"]:
                if label < byte:
                    result += self.levels[dest].get(p / factor, 0)
                elif label == byte:
                    node, p = dest, p / factor
                    break
            else:
                raise AssertionError("membership and rank disagree")
        return result

    def iter_range(self, start, stop):
        if not 0 <= start <= stop <= self.count:
            raise ValueError("unique range outside snapshot")
        for rank in range(start, stop):
            yield rank, *self.at(rank)

    def as_stream(self, snapshot_id=None):
        """Use the existing allocation Leaf/Mix protocol, without replacing it."""
        return ProbabilityStream(snapshot_id or identity(self.payload()), self)

    def counts_by_length(self):
        memo = {}

        def visit(node):
            if node not in memo:
                counts = Counter({0: 1}) if self.nodes[node]["final"] else Counter()
                for _, dest, _ in self.nodes[node]["edges"]:
                    counts.update({n + 1: count for n, count in visit(dest).items()})
                memo[node] = counts
            return memo[node]
        return dict(visit(0))

    def payload(self):
        return {"schema": "unique-rational-dag-v1", "order": "total-probability-desc/bytes-lex",
            "initial": str(self.initial), "nodes": [{"final": str(n["final"]),
                "edges": [[b, d, str(p)] for b, d, p in n["edges"]]} for n in self.nodes]}

    @classmethod
    def from_payload(cls, payload):
        if payload["schema"] != "unique-rational-dag-v1" or payload["order"] != "total-probability-desc/bytes-lex":
            raise Unsupported("unknown snapshot semantics")
        nodes = [{"final": Q(n["final"]), "edges": [(b, d, Q(p)) for b, d, p in n["edges"]]} for n in payload["nodes"]]
        for node in nodes:
            labels = [b for b, _, _ in node["edges"]]
            if labels != sorted(set(labels)) or node["final"] < 0:
                raise ValueError("invalid deterministic node")
            if any(not 32 <= b <= 126 or not 0 <= d < len(nodes) or p <= 0 for b, d, p in node["edges"]):
                raise ValueError("invalid deterministic transition")
        return cls(nodes, Q(payload["initial"]))


def compile_probability(spec, limits=Limits()):
    if spec.get("schema_version") != 1 or spec.get("mode") != "probability":
        raise Unsupported("probability compilation requires explicit probability semantics")
    validate(spec["root"])
    nfa = NFA(limits)
    nfa.emit(spec["root"], nfa.start, nfa.final)
    result = nfa.determinize()
    result.model_id = identity(spec)
    return result


@dataclass(frozen=True)
class ProbabilityOccurrence:
    snapshot: str
    rank: int
    value: bytes
    probability: Q


@dataclass(frozen=True)
class ProbabilityStream:
    id: str
    index: UniqueIndex

    @property
    def count(self):
        return self.index.count

    def iter_range(self, start, stop):
        for rank, value, probability in self.index.iter_range(start, stop):
            yield ProbabilityOccurrence(self.id, rank, value, probability)


CAPABILITIES = {
    "mode": "probability", "deterministic": True, "exact_unique_outputs": True,
    "ranking": "exact descending total string probability, byte-lexicographic ties",
    "direct_access": True, "inverse_bytes": True, "length_counts": True,
    "cost_counts": "exact probability bands, not quantized costs",
    "limits": "Only successful bounded acyclic compilation; state, context, rational and score-table growth are limited.",
}
