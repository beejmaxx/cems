"""Human-editable recipes, compiled to the existing exact construction language.

Weights describe generative choices, not measured recovery odds. Template lengths
refer to source chunks before optional edits; deletion happens after casing.
"""
from collections import defaultdict
import copy
from fractions import Fraction as Q
import itertools
import math
from pathlib import Path
import re
import tomllib

from emitter_v1 import model as m
from model_workflow import SCHEMA

SCHEMA_RECIPE = "recollect-recipe-v1"
MAX_RECIPE = 256 * 1024
TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")
MODES = {"shared-pattern", "independent-pattern", "shared-style", "independent-style", "literal"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def fields(value, allowed, context):
    require(isinstance(value, dict), context + " must be a table")
    require(set(value) <= set(allowed), context + ": unknown field(s): " + ", ".join(sorted(set(value) - set(allowed))))


def weight(value):
    require(type(value) in (int, str), "weights must be integers or quoted fractions/decimals")
    result = Q(value)
    require(result > 0 and max(result.numerator.bit_length(), result.denominator.bit_length()) <= 128,
            "weights must be positive, bounded exact numbers")
    return result


def pick(rows):
    require(bool(rows), "choice has no alternatives")
    return m.choice([(str(i), m.literal(value), w) for i, (value, w) in enumerate(rows)])


def integer_rows(rows):
    denominator = math.lcm(*(w.denominator for _, w in rows))
    numbers = [int(w * denominator) for _, w in rows]
    divisor = math.gcd(*numbers)
    return [(v, n // divisor) for (v, _), n in zip(rows, numbers)]


def options(slot):
    rows = slot.get("options", [])
    require(isinstance(rows, list) and 0 < len(rows) <= 4096, "finite slot needs 1..4096 options")
    result = []
    for row in rows:
        fields(row, {"value", "weight"}, "slot option")
        m.ascii_bytes(row["value"])
        result.append((row["value"], weight(row.get("weight", 1))))
    require(len({v for v, _ in result}) == len(result), "duplicate slot option; express its weight explicitly")
    return result


def style_word(value, style):
    if style is None:
        return value
    if isinstance(style, tuple):
        pattern = style[1]
        require(len(value) == len(pattern), "case pattern width differs from source chunk")
        return "".join(c.upper() if p == "u" else c.lower() for c, p in zip(value, pattern))
    return {"lower": str.lower, "title": str.title, "upper": str.upper}[style](value)


def finite_rows(slot, width=None, style=None, only=None):
    rows = [(style_word(v, style), w) for v, w in options(slot)
            if (width is None or len(v) == width) and (only is None or v == only)]
    require(rows, "slot has no alternatives at this source length")
    transforms = slot.get("transforms")
    if not transforms:
        return rows
    require(isinstance(transforms, list) and 1 <= len(transforms) <= 16, "invalid transforms")
    operations = []
    for transform in transforms:
        fields(transform, {"op", "weight"}, "transform")
        require(transform["op"] in {"identity", "lower", "title", "upper", "reverse", "delete-one", "shift-number-row"}, "unknown transform")
        operations.append((transform["op"], weight(transform.get("weight", 1))))
    total = sum(w for _, w in operations)
    merged = defaultdict(Q)
    shift = str.maketrans("1234567890!@#$%^&*()", "!@#$%^&*()1234567890")
    for value, base_weight in rows:
        for op, w in operations:
            p = base_weight * w / total
            if op == "delete-one":
                require(bool(value), "delete-one cannot apply to an empty source string")
                for i in range(len(value)):
                    merged[value[:i] + value[i+1:]] += p / len(value)
            else:
                result = value.translate(shift) if op == "shift-number-row" else m.apply_transform(value.encode(), op).decode()
                merged[result] += p
    return integer_rows(sorted(merged.items()))


def marked(expr, **metadata):
    """Inspection-only wrapper. Ordinary compiled recipes never contain these."""
    return dict(m.concat(expr), desk=metadata)


def annotated_slot(slot, width=None, style=None, only=None):
    """Keep latent source/edit explanations that finite_rows normally merges.

    This is a second representation for inspection, with the SAME output
    distribution. It never supplies the campaign compiler's input.
    """
    if "alphabet" in slot:
        return marked(slot_expr(slot, width, style), source=None, alphabet=slot["alphabet"], width=width)
    rows = [(v, w) for v, w in options(slot)
            if (width is None or len(v) == width) and (only is None or v == only)]
    choices = []
    shift = str.maketrans("1234567890!@#$%^&*()", "!@#$%^&*()1234567890")
    for i, (source, base_weight) in enumerate(rows):
        value = style_word(source, style)
        edits = []
        for j, tr in enumerate(slot.get("transforms") or [{"op": "identity", "weight": 1}]):
            op = tr["op"]
            if op == "delete-one":
                children = [(str(k), marked(m.literal(value[:k]+value[k+1:]), deletion_position=k+1), 1)
                            for k in range(len(value))]
                child = m.choice(children)
            else:
                output = value.translate(shift) if op == "shift-number-row" else m.apply_transform(value.encode(), op).decode()
                child = m.literal(output)
            edits.append((str(j), marked(child, transform=op), weight(tr.get("weight", 1))))
        choices.append((str(i), marked(m.choice(edits), source=source, style=style), base_weight))
    return m.choice(choices)


def slot_expr(slot, width=None, style=None, only=None, *, annotate=False):
    if annotate:
        return annotated_slot(slot, width, style, only)
    if "alphabet" not in slot:
        return pick(finite_rows(slot, width, style, only))
    alphabet = slot["alphabet"]
    require(width is not None and type(width) is int and 1 <= width <= 16, "alphabet slots need a source length 1..16")
    require(not slot.get("transforms"), "alphabet transforms are not supported; use finite options for deletion rules")
    if style is None or style == "lower":
        return m.words(alphabet, width)
    if style == "upper":
        return m.words(alphabet.upper(), width)
    if style == "title":
        parts = [m.words(alphabet.upper(), 1)]
        if width > 1:
            parts.append(m.words(alphabet, width - 1))
        return m.concat(*parts)
    return m.concat(*(m.words(alphabet.upper() if c == "u" else alphabet, 1) for c in style[1]))


def pattern_rows(width, case):
    rows, other = [], []
    for bits in itertools.product("lu", repeat=width):
        pattern = "".join(bits)
        category = ("lower" if pattern == "l" * width else "title" if pattern == "u" + "l" * (width-1)
                    else "upper" if pattern == "u" * width else "mixed")
        row = (pattern, weight(case[category]))
        (other if category == "mixed" else rows).append(row)
    if case["mixed_budget"] == "family" and other:
        other = [(p, w / len(other)) for p, w in other]
    return sorted(rows + other)


def parse_pattern(pattern, chunks, slots):
    require(isinstance(pattern, str) and len(pattern) <= 1024, "invalid template pattern")
    parts, pos = [], 0
    for match in TOKEN.finditer(pattern):
        if match.start() > pos:
            parts.append((False, pattern[pos:match.start()]))
        name = match[1]
        require(name in slots or name == "last_sep" or name in {f"chunk{i+1}" for i in range(chunks)}, "unknown placeholder: " + name)
        require(name not in slots or "alphabet" not in slots[name], "alphabet slots must be named in chunks so their source length is defined")
        parts.append((True, name)); pos = match.end()
    if pos < len(pattern):
        parts.append((False, pattern[pos:]))
    require(all("{" not in v and "}" not in v for is_ref, v in parts if not is_ref), "unmatched braces in pattern")
    require(all(sum(is_ref and v == f"chunk{i+1}" for is_ref, v in parts) == 1 for i in range(chunks)),
            "each chunk must occur exactly once; reorder chunk placeholders to swap words")
    return parts


def template_root(template, slots, case, width=None, pairs=None, *, annotate=False):
    mode = template.get("case", "literal")
    bindings = []
    shared = template.get("separators", "shared") == "shared"
    if shared:
        bindings.append(("separator", {"op": "component", "name": "separator"}))
    bindings.append(("last", {"op": "component", "name": "last-boundary"}))
    styles = [(s, weight(case[s])) for s in ("lower", "title", "upper")]
    if mode == "shared-pattern":
        require(width is not None and pairs is None, "shared-pattern requires equal source lengths")
        bindings.append(("pattern", pick(pattern_rows(width, case))))
    elif mode == "shared-style":
        bindings.append(("style", pick(styles)))
    chunks = [slots[name] for name in template.get("chunks", [])]

    def group(i, length=width, only=None):
        slot = chunks[i]
        if mode.endswith("pattern"):
            rows = pattern_rows(length, case)
            variable = "pattern" if mode == "shared-pattern" else f"case_{i}"
            expr = m.conditional(variable, {p: slot_expr(slot, length, ("pattern", p), only, annotate=annotate) for p, _ in rows})
            return expr if mode == "shared-pattern" else m.let([(variable, pick(rows))], expr)
        if mode.endswith("style"):
            variable = "style" if mode == "shared-style" else f"case_{i}"
            expr = m.conditional(variable, {s: slot_expr(slot, length, s, only, annotate=annotate) for s, _ in styles})
            return expr if mode == "shared-style" else m.let([(variable, pick(styles))], expr)
        return slot_expr(slot, length, None, only, annotate=annotate)

    parts = parse_pattern(template["pattern"], len(chunks), slots)

    def separator():
        return m.ref("separator") if shared else {"op": "component", "name": "separator"}

    def render(items, group_exprs):
        result = []
        for is_ref, value in items:
            if not is_ref:
                expr, slot = m.literal(value), None
            elif value.startswith("chunk") and value[5:].isdigit():
                i = int(value[5:])-1
                expr, slot = group_exprs[i], template["chunks"][i]
            elif value == "separator":
                expr, slot = separator(), "separator"
            elif value == "last_sep":
                expr, slot = m.conditional("last", {"keep": separator(), "omit": m.literal("")}), "separator"
            else:
                expr, slot = {"op": "component", "name": value}, value
            result.append(marked(expr, role=value if is_ref else "literal", slot=slot) if annotate else expr)
        return result

    if pairs is None:
        output = m.concat(*render(parts, [group(i) for i in range(len(chunks))]))
    else:
        positions = [i for i, (r, v) in enumerate(parts) if r and v.startswith("chunk") and v[5:].isdigit()]
        first, last = min(positions), max(positions)
        options_out = []
        for n, (pair, w) in enumerate(pairs):
            exprs = [group(i, v if isinstance(v, int) else None, None if isinstance(v, int) else v) for i, v in enumerate(pair)]
            options_out.append((str(n), m.concat(*render(parts[first:last+1], exprs)), w))
        output = m.concat(*render(parts[:first], []), m.choice(options_out), *render(parts[last+1:], []))
    return m.let(bindings, output)


def compile_recipe(recipe, *, annotate=False):
    fields(recipe, {"schema", "name", "status", "notes", "source", "families", "case", "relationships", "lengths", "slots", "templates", "history"}, "recipe")
    require(recipe.get("schema") == SCHEMA_RECIPE and recipe.get("status") == "proposal", "recipe must use recollect-recipe-v1 and status='proposal'")
    slots, templates = recipe.get("slots", {}), recipe.get("templates", [])
    require(isinstance(slots, dict) and 1 <= len(slots) <= 64, "recipe needs 1..64 named slots")
    for name, slot in slots.items():
        require(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9-]*", name) and not name.startswith("chunk") and name not in {"last_sep", "last-boundary"}, "invalid/reserved slot name")
        fields(slot, {"options", "alphabet", "transforms", "evidence", "notes"}, "slot " + name)
        if "alphabet" in slot:
            require("options" not in slot, "slot must have options or alphabet, not both")
            require(not slot.get("transforms"), "alphabet transforms are not supported; use finite options for deletion rules")
            alphabet = slot["alphabet"]
            m.ascii_bytes(alphabet)
            require(bool(alphabet) and len(set(alphabet)) == len(alphabet) and alphabet.isalpha() and alphabet.islower(), "alphabet must have unique lowercase ASCII letters")
        else:
            finite_rows(slot)
    require("separator" in slots and "alphabet" not in slots["separator"], "define slots.separator with finite options")
    case = recipe.get("case", {})
    fields(case, {"lower", "title", "upper", "mixed", "mixed_budget", "notes"}, "case")
    require(case.get("mixed_budget") in {"per-pattern", "family"}, "case.mixed_budget must be per-pattern or family")
    for k in ("lower", "title", "upper", "mixed"):
        weight(case[k])
    lengths = recipe.get("lengths", {})
    for name, values in lengths.items():
        require(isinstance(values, dict) and bool(values), "lengths must be named nonempty tables")
        for length, w in values.items():
            require(str(int(length)) == length and 1 <= int(length) <= 8, "source length must be 1..8")
            weight(w)
    require(isinstance(templates, list) and 0 < len(templates) <= 32 and all(isinstance(t, dict) for t in templates), "recipe needs 1..32 template tables")
    relationships = recipe.get("relationships", {})
    fields(relationships, {"separators", "equal_case", "other_case"}, "relationships")
    require(relationships.get("separators", "shared") in {"shared", "independent"}, "invalid default separator relationship")
    require(all(v in MODES for k, v in relationships.items() if k != "separators"), "invalid default case relationship")
    templates = copy.deepcopy(templates)
    for t in templates:
        t.setdefault("separators", relationships.get("separators", "shared"))
        key = "equal_case" if t.get("lengths") == "equal" else "other_case"
        t.setdefault("case", relationships.get(key, "literal"))
    families = recipe.get("families", {"default": 1})
    require(isinstance(families, dict) and families, "invalid families")
    families = {k: weight(v) for k, v in families.items()}
    totals = defaultdict(Q)
    ids = set()
    for t in templates:
        fields(t, {"id", "family", "weight", "pattern", "chunks", "lengths", "length_table", "case", "separators", "last_separator", "evidence", "notes"}, "template")
        name = t.get("id")
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", name) and name not in ids, "unique safe template id required")
        ids.add(name)
        family = t.get("family", "default")
        require(family in families, "template names an unknown family")
        totals[family] += weight(t.get("weight", 1))
        require(t.get("case", "literal") in MODES, "unsupported case relationship")
        require(t.get("separators", "shared") in {"shared", "independent"}, "separator relationship must be shared or independent")
        require(isinstance(t.get("chunks", []), list) and len(t.get("chunks", [])) <= 3 and all(s in slots for s in t.get("chunks", [])), "chunks must name 0..3 slots")
        require(t.get("lengths", "any") in {"any", "equal", "unequal"}, "invalid length relationship")
        require(t.get("lengths", "any") == "any" or t.get("length_table") in lengths, "select a named length_table")
        parse_pattern(t["pattern"], len(t.get("chunks", [])), slots)
    require(set(totals) == set(families), "every weighted family needs a template")
    components = {name: annotated_slot(slot) if annotate else pick(finite_rows(slot))
                  for name, slot in slots.items() if "alphabet" not in slot}
    components["last-boundary"] = pick([("keep", 8), ("omit", 1)])
    branches = []
    for t in templates:
        family = t.get("family", "default")
        template_weight = families[family] * weight(t.get("weight", 1)) / totals[family]
        chunks = [slots[n] for n in t.get("chunks", [])]
        relation = t.get("lengths", "any")
        last = t.get("last_separator", {"keep": 8, "omit": 1})
        fields(last, {"keep", "omit"}, "last_separator")
        last_expr = pick([(k, weight(last[k])) for k in ("keep", "omit")])
        variants = []
        if relation == "equal":
            admitted = [(int(n), weight(w)) for n, w in lengths[t["length_table"]].items()
                        if all("alphabet" in s or any(len(v) == int(n) for v, _ in options(s)) for s in chunks)]
            require(bool(admitted), "no admitted equal source lengths in template " + t["id"])
            total = sum(w for _, w in admitted)
            for n, w in admitted:
                variants.append((f"{t['id']}-length-{n}", w/total, template_root(t, slots, case, width=n, annotate=annotate)))
        elif relation == "any":
            require(all("alphabet" not in s for s in chunks), "any-length templates require finite chunks")
            require(t.get("case", "literal") not in {"shared-pattern", "independent-pattern"}, "any-length template requires a style or literal casing")
            variants.append((t["id"], Q(1), template_root(t, slots, case, annotate=annotate)))
        else:
            require(len(chunks) == 2, "unequal length templates require exactly two chunks")
            require(t.get("case", "literal") not in {"shared-pattern", "independent-pattern"}, "unequal template requires a style or literal casing")
            domains = [[(int(n), weight(w)) for n, w in lengths[t["length_table"]].items()] if "alphabet" in s else options(s) for s in chunks]
            length = lambda v: v if isinstance(v, int) else len(v)
            pairs = [((a, b), aw*bw) for a, aw in domains[0] for b, bw in domains[1] if length(a) != length(b)]
            require(pairs, "no unequal source lengths")
            total = sum(w for _, w in pairs)
            buckets = defaultdict(list)
            for pair, w in pairs:
                buckets[str(length(pair[0])) if "alphabet" in chunks[0] else "all"].append((pair, w))
            for key, rows in buckets.items():
                name = t["id"] if key == "all" else t["id"] + "-first-" + key
                variants.append((name, sum(w for _, w in rows)/total, template_root(t, slots, case, pairs=rows, annotate=annotate)))
        for name, portion, root in variants:
            for binding in root["bindings"]:
                if binding["name"] == "last":
                    # Keep the common component reference when identical: this also
                    # preserves graph cache identities for unchanged recipes.
                    if last_expr != components["last-boundary"]:
                        binding["expr"] = last_expr
            branches.append({"id": name, "weight": str(template_weight * portion), "root": root,
                "evidence": {"family": family, "template": t["id"], "sources": t.get("evidence", []),
                             "proposal_status": "unadopted", "numerical_weight_status": "sensitivity experiment",
                             "notes": t.get("notes", "")}})
    require(len(branches) <= 32, "recipe expands to more than 32 compiled branches; reduce template/length combinations")
    return {"schema": SCHEMA, "description": "Unadopted recipe: " + recipe.get("name", "review"),
            "components": components, "hypotheses": branches}


def read_recipe(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_RECIPE + 1)
    require(len(raw) <= MAX_RECIPE, "recipe exceeds 256 KiB")
    return tomllib.loads(raw.decode("utf-8"))


def main():
    import argparse
    import json
    import os
    import shlex
    from workspace import Workspace
    parser = argparse.ArgumentParser(prog="cems recipe", description=__doc__)
    parser.add_argument("command", choices=("edit", "path", "check", "show", "export"))
    parser.add_argument("file", nargs="?", type=Path, help="defaults to workspace models/RECIPE.toml")
    parser.add_argument("--output", type=Path, help="new construction JSON path for export")
    args = parser.parse_args()
    path = (args.file or Workspace().resolve("models/RECIPE.toml")).expanduser().resolve()
    if args.command == "path":
        print(path); return
    if args.command == "edit":
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nvim")
        require(bool(editor), "editor is empty")
        os.execvp(editor[0], [*editor, str(path)])
    recipe = read_recipe(path)
    spec = compile_recipe(recipe)
    if args.command == "export":
        require(args.output is not None, "export requires --output NEW.json")
        from model_workflow import save_new
        save_new(args.output, spec)
        print(args.output); return
    print(f"PROPOSAL: {recipe.get('name', path.name)}\nRecipe: {path}")
    print(f"{len(recipe['templates'])} templates → {len(spec['hypotheses'])} compiled branches")
    total = sum(weight(w) for w in recipe.get("families", {"default": 1}).values())
    print("\nFamily allocations (experimental; before history exclusions):")
    for name, w in recipe.get("families", {"default": 1}).items():
        print(f"  {name}: {float(100*weight(w)/total):.2f}%  (weight {w})")
    if args.command == "show":
        print("\nRules:")
        for t in recipe["templates"]:
            relationships = recipe.get("relationships", {})
            default_case = relationships.get("equal_case" if t.get("lengths") == "equal" else "other_case", "literal")
            print(f"  {t['id']}: {t['pattern']}\n    chunks={t.get('chunks', [])}; lengths={t.get('lengths', 'any')}; "
                  f"case={t.get('case', default_case)}; separators={t.get('separators', relationships.get('separators', 'shared'))}")
        print("\nCapitalization (conditional on source length; equal-length pattern modes):")
        for length in range(1, 5):
            rows = pattern_rows(length, recipe["case"])
            z = sum(w for _, w in rows)
            plain = dict(rows)["l"*length]/z
            mixed = sum(w for p, w in rows if p not in {"l"*length, "u"+"l"*(length-1), "u"*length})/z
            print(f"  {length} letters: lowercase {float(100*plain):.2f}%; all other mixed patterns together {float(100*mixed):.2f}%")
        if recipe.get("history"):
            print("\nHistory policy: " + recipe["history"]["policy"])
    print("\nWeights validated. This command does not compile, check passwords, or adopt a model.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        raise SystemExit("cems recipe: " + str(error)) from None
