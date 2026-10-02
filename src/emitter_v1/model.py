"""JSON-native construction language. Ordinal costs and probabilities never mix.

Conditions observe bound bytes, not hidden explanation identity. If latent
explanations have different futures, bind their identity explicitly or keep them
in separate branches. Evidence metadata does not silently become a probability.
"""
from fractions import Fraction
from copy import deepcopy
import hashlib
import json


class Unsupported(ValueError):
    pass


class LimitExceeded(Unsupported):
    pass


def rational(value):
    if isinstance(value, (float, bool)):
        raise ValueError("use exact integer or fraction/decimal string weights, not floats")
    result = Fraction(value)
    if result <= 0:
        raise ValueError("weights must be positive")
    return result


def ascii_bytes(value):
    if not isinstance(value, str) or any(not 32 <= ord(c) <= 126 for c in value):
        raise Unsupported("this version accepts printable ASCII, including the empty string")
    return value.encode("ascii")


def literal(value):
    ascii_bytes(value)
    return {"op": "literal", "value": value}


def words(alphabet, length):
    ascii_bytes(alphabet)
    if not alphabet or len(set(alphabet)) != len(alphabet) or type(length) is not int or not 1 <= length <= 128:
        raise ValueError("invalid bounded word domain")
    return {"op": "words", "alphabet": alphabet, "length": length}


def choice(options, mode="probability"):
    """options: (stable explanation id, child expression, weight OR ordinal cost)."""
    if mode not in {"probability", "ordinal_compat"}:
        raise ValueError("unknown score semantics")
    entries = []
    for id, expr, score in options:
        if not isinstance(id, str) or not id:
            raise ValueError("choice explanations require stable nonempty ids")
        if mode == "probability":
            field, value = "weight", str(rational(score))
        else:
            if type(score) is not int or score < 0:
                raise ValueError("ordinal cost must be a nonnegative integer")
            field, value = "cost", score
        entries.append({"id": id, "expr": expr, field: value})
    if not entries or len({e["id"] for e in entries}) != len(entries):
        raise ValueError("empty choice or repeated explanation id")
    return {"op": "choice", "mode": mode, "options": entries}


def concat(*parts):
    return {"op": "concat", "parts": list(parts)}


def ref(name):
    return {"op": "ref", "name": name}


def transform(expr, operation):
    if isinstance(operation, str) and operation not in {"identity", "lower", "upper", "title", "reverse"}:
        raise Unsupported("unknown transform")
    return {"op": "transform", "expr": expr, "operation": operation}


def conditional(selector, cases):
    return {"op": "conditional", "selector": selector, "cases": dict(cases)}


def let(bindings, output, name="construction", base_cost=0):
    names = [n for n, _ in bindings]
    if len(names) != len(set(names)) or any(not isinstance(n, str) or not n for n in names):
        raise ValueError("binding names must be unique and nonempty")
    return {"op": "let", "name": name, "bindings": [{"name": n, "expr": e} for n, e in bindings],
            "output": output, "base_cost": base_cost}


def model(root, mode="probability", evidence=None):
    if mode not in {"probability", "ordinal_compat"}:
        raise ValueError("unknown model mode")
    return {"schema_version": 1, "mode": mode, "root": root, "evidence": evidence or {},
            "interpretation": "Subjective model weights, not measured recovery odds; evidence metadata is not a score."}


def with_evidence(expr, *, sources=(), confidence="unspecified", rationale=""):
    """Attach provenance without changing output weights or ordinal costs."""
    result = deepcopy(expr)
    result["evidence"] = {"sources": list(sources), "confidence_judgment": confidence,
                          "rationale": rationale, "scoring_effect": "none; weights must be specified explicitly"}
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def identity(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def apply_transform(value, operation):
    if operation == "identity":
        return value
    if operation == "lower":
        return value.lower()
    if operation == "upper":
        return value.upper()
    if operation == "title":
        return value[:1].upper() + value[1:].lower()
    if operation == "reverse":
        return value[::-1]
    raise Unsupported("unknown transform")
