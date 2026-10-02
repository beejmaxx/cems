"""Artificial recollections and an independent compact recipe oracle (TEST ONLY).

Oracle languages have one fixed-width decimal run with nonnumeric text on either
side. Full strings need not be stored: a recipe is (before, number, after).
This restricted test oracle is NOT the native worker or its history mechanism.
"""
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction as Q
import itertools
from pathlib import Path
import random

from runner import require

from emitter_v1.model import choice, concat, conditional, let, literal, ref, transform, words


@dataclass(frozen=True)
class Configuration:
    digits: int = 8
    prefixes: tuple = ("!", "~", "@@")
    trees: tuple = ("ash", "birch", "cedar", "maple", "rowan", "willow")
    concepts: tuple = ("axis", "delta", "orbit", "slope", "tensor", "vector")
    separators: tuple = ("#", "-", "_")
    endings: tuple = ("?", "!!", "._")
    number_heads: tuple = ("2718", "2817", "1728")


SMALL = Configuration(2, ("!", "~"), ("ash", "birch"), ("axis", "orbit"), ("#", "-"), ("?", "!!"), ("2", "3", "7"))
DESCRIPTIONS = (
    "Initial uncertainty: two words followed by a number; casing and separator uncertain.",
    "New idea: the number may be between the words; separators may have been chosen independently.",
    "Misleading recollection: commit to uppercase and favor swapped words; temporarily withdraw the true construction.",
    "Withdraw the misleading clue, restore earlier ideas, and retain swapped-word alternatives at lower weight.",
    "New numeric-memory clue: several possible leading digit groups; title case and a reused separator gain weight.",
)


def policy(stage):
    require(0 <= stage < 5, "unknown artificial recollection stage")
    # (name, branch weight, (lower,title,caps), order weights, shared?, swapped?, focused digits?)
    if stage == 0:
        return [("end-shared", 1, (6, 1, 2), (("end", 1),), True, False, False)]
    if stage == 2:
        return [("end-uppercase", 1, (0, 0, 1), (("end", 1),), True, False, False),
                ("swapped-uppercase", 9, (0, 0, 1), (("middle", 1),), True, True, False)]
    result = [("end-shared", 1, (8, 1, 2), (("end", 1),), True, False, False),
              ("flexible-independent", 6 if stage < 4 else 3, (9, 1, 4), (("end", 1), ("middle", 3)), False, False, False),
              ("middle-shared", 3 if stage < 4 else 2, (8, 1, 2), (("middle", 1),), True, False, False)]
    if stage >= 3:
        result.append(("swapped-shared", 1, (8, 1, 2), (("middle", 1),), True, True, False))
    if stage == 4:
        result.append(("numeric-memory", 50, (1, 8, 1), (("middle", 1),), True, False, True))
    return result


def inputs(stage, cfg=Configuration()):
    """No password or seed is accepted. The entire schedule is fixed beforehand."""
    def component(name):
        return {"op": "component", "name": name}

    def uniform(values):
        return choice([(str(i), literal(v), 1) for i, v in enumerate(values)])

    components = {"prefix": uniform(cfg.prefixes), "tree": uniform(cfg.trees), "concept": uniform(cfg.concepts),
                  "separator": uniform(cfg.separators), "ending": uniform(cfg.endings),
                  "number": words("0123456789", cfg.digits),
                  "number-clue": choice([(f"head-{i}", concat(literal(head), words("0123456789", cfg.digits-len(head))), w)
                                         for i, (head, w) in enumerate(zip(cfg.number_heads, (3, 2, 1)))])}
    hypotheses = []
    for name, weight, style_weights, orders, shared, swapped, focused in policy(stage):
        styles = choice([(s, literal(s), w) for s, w in zip(("lower", "title", "caps"), style_weights) if w])
        tree = conditional("style", {"lower": component("tree"), "title": transform(component("tree"), "title"),
                                     "caps": transform(component("tree"), "upper")})
        concept = conditional("style", {"lower": component("concept"), "title": component("concept"),
                                        "caps": transform(component("concept"), "upper")})
        first, second = (concept, tree) if swapped else (tree, concept)
        sep1 = ref("separator") if shared else component("separator")
        sep2 = ref("separator") if shared else component("separator")
        number = component("number-clue" if focused else "number")
        layouts = []
        for order, order_weight in orders:
            parts = (first, sep1, second, sep2, number) if order == "end" else (first, sep1, number, sep2, second)
            layouts.append((order, concat(component("prefix"), *parts, component("ending")), order_weight))
        bindings = [("style", styles)] + ([("separator", component("separator"))] if shared else [])
        hypotheses.append({"id": name, "weight": weight, "root": let(bindings, choice(layouts)),
                           "evidence": {"scope": "engineered test assumption, not personal memory", "stage": DESCRIPTIONS[stage]}})
    return {"schema": "search-constructions-v1", "description": DESCRIPTIONS[stage],
            "components": components, "hypotheses": hypotheses}


def secret(seed, cfg=Configuration()):
    rng = random.Random(seed)
    recipe = {"prefix": rng.choice(cfg.prefixes), "tree": rng.choice(cfg.trees).title(),
              "concept": rng.choice(cfg.concepts), "separator": rng.choice(cfg.separators),
              "number": cfg.number_heads[0] + f"{rng.randrange(10**(cfg.digits-len(cfg.number_heads[0]))):0{cfg.digits-len(cfg.number_heads[0])}d}",
              "ending": rng.choice(cfg.endings)}
    password = (recipe["prefix"] + recipe["tree"] + recipe["separator"] + recipe["number"] +
                recipe["separator"] + recipe["concept"] + recipe["ending"]).encode("ascii")
    return password, recipe


class RecipeModel:
    """Independent arithmetic on recipes, without emitter graphs or plan parsing."""
    def __init__(self, stage, cfg=Configuration()):
        self.cfg = cfg
        events = defaultdict(lambda: defaultdict(Q))
        branches = policy(stage)
        total_weight = sum(b[1] for b in branches)
        self.derivations = 0
        for _, branch_weight, style_weights, orders, shared, swapped, focused in branches:
            pairs = [(s, s) for s in cfg.separators] if shared else list(itertools.product(cfg.separators, repeat=2))
            number_ranges = [(0, 10**cfg.digits, Q(1))]
            if focused:
                number_ranges = [(int(head)*10**(cfg.digits-len(head)), (int(head)+1)*10**(cfg.digits-len(head)), Q(w, 6))
                                 for head, w in zip(cfg.number_heads, (3, 2, 1))]
            for prefix, tree, concept, (s1, s2), ending, style, (order, ow) in itertools.product(
                    cfg.prefixes, cfg.trees, cfg.concepts, pairs, cfg.endings, range(3), orders):
                if not style_weights[style]:
                    continue
                first = tree if style == 0 else tree.title() if style == 1 else tree.upper()
                second = concept.upper() if style == 2 else concept
                if swapped:
                    first, second = second, first
                if order == "end":
                    before, after = prefix + first + s1 + second + s2, ending
                else:
                    before, after = prefix + first + s1, s2 + second + ending
                key = (before.encode("ascii"), after.encode("ascii"))
                require(not any(48 <= c <= 57 for text in key for c in text), "oracle requires one unambiguous digit run")
                base = Q(branch_weight, total_weight) * Q(style_weights[style], sum(style_weights)) * Q(ow, sum(w for _, w in orders))
                base /= len(cfg.prefixes)*len(cfg.trees)*len(cfg.concepts)*len(pairs)*len(cfg.endings)
                for lo, hi, number_weight in number_ranges:
                    p = base * number_weight / (hi-lo)
                    events[key][lo] += p
                    events[key][hi] -= p
                    self.derivations += hi-lo
        self.pieces, total_mass = {}, Q(0)
        for key, changes in events.items():
            score, previous, pieces = Q(0), None, []
            for point, change in sorted(changes.items()):
                if previous is not None and previous < point and score:
                    require(score > 0, "negative oracle mass")
                    pieces.append((previous, point, score))
                    total_mass += (point-previous)*score
                score += change
                previous = point
            require(score == 0, "unbalanced oracle mass events")
            self.pieces[key] = pieces
        require(total_mass == 1, "independent model probability mass is not one")

    def parse(self, value):
        start = next((i for i, c in enumerate(value) if 48 <= c <= 57), None)
        if start is None:
            return None
        end = start
        while end < len(value) and 48 <= value[end] <= 57:
            end += 1
        if end-start != self.cfg.digits or any(48 <= c <= 57 for c in value[end:]):
            return None
        return (value[:start], value[end:]), int(value[start:end])

    def score(self, value):
        parsed = self.parse(value)
        if parsed is None:
            return Q(0)
        key, number = parsed
        return next((p for a, b, p in self.pieces.get(key, ()) if a <= number < b), Q(0))


def subtract(interval, completed):
    lo, hi = interval
    for a, b in completed:
        if b <= lo:
            continue
        if a >= hi:
            break
        if lo < a:
            yield lo, a
        lo = max(lo, b)
        if lo >= hi:
            break
    if lo < hi:
        yield lo, hi


class RecipeHistory:
    """TEST oracle: numeric intervals per exact text skeleton, not candidate keys."""
    def __init__(self):
        self.ranges = {}
        self.count = 0

    def add(self, index, start, count):
        stop, added = start + count, 0
        require(0 <= start <= stop <= index.count, "oracle completion outside index")
        for s, offset in zip(index.segments, index.offsets):
            a, b = max(start, offset)-offset, min(stop, offset+s.count)-offset
            if a >= b:
                continue
            m = len(s.afters)
            for j, after in enumerate(s.afters):
                lo = s.lo + max(0, (a-j+m-1)//m)
                hi = s.lo + min(s.hi-s.lo, (b-j+m-1)//m)
                if lo >= hi:
                    continue
                key = s.before, after
                old = self.ranges.get(key, [])
                fresh = sum(y-x for x, y in subtract((lo, hi), old))
                require(fresh == hi-lo, "acknowledged candidate checked twice according to independent history")
                merged = []
                for x, y in sorted([*old, (lo, hi)]):
                    if merged and x <= merged[-1][1]:
                        merged[-1] = merged[-1][0], max(y, merged[-1][1])
                    else:
                        merged.append((x, y))
                self.ranges[key] = merged
                added += fresh
        require(added == count, "oracle history update lost candidates")
        self.count += added


@dataclass(frozen=True)
class Segment:
    before: bytes
    lo: int
    hi: int
    afters: tuple
    score: Q

    @property
    def count(self):
        return (self.hi-self.lo)*len(self.afters)


class RecipeIndex:
    """Piecewise-uniform ranking: score, before-text, number, after-text.

    Before/after contain no digits. Therefore different before-text groups do
    not interleave: their relative order is determined by before + '0'*width.
    Within a group numeric intervals and suffix lists give direct rank/unrank.
    """
    def __init__(self, model, history=None):
        self.model = model
        groups = defaultdict(lambda: defaultdict(list))
        for (before, after), pieces in model.pieces.items():
            done = history.ranges.get((before, after), []) if history else []
            for lo, hi, p in pieces:
                for a, b in subtract((lo, hi), done):
                    groups[(p, before)][a].append((after, 1))
                    groups[(p, before)][b].append((after, -1))
        self.segments, self.offsets, self.lookup, self.count = [], [], defaultdict(list), 0
        for (p, before), changes in sorted(groups.items(), key=lambda item: (-item[0][0], item[0][1]+b"0"*model.cfg.digits)):
            active, last = set(), None
            for point, delta in sorted(changes.items()):
                if last is not None and point > last and active:
                    s = Segment(before, last, point, tuple(sorted(active)), p)
                    self.lookup[(p, before)].append((s, self.count))
                    self.segments.append(s)
                    self.offsets.append(self.count)
                    self.count += s.count
                net = defaultdict(int)
                for after, change in delta:
                    net[after] += change
                for after, change in net.items():
                    if change == 1:
                        require(after not in active, "overlapping oracle recipe interval")
                        active.add(after)
                    elif change == -1:
                        require(after in active, "unbalanced oracle interval")
                        active.remove(after)
                    else:
                        require(change == 0, "invalid oracle interval multiplicity")
                last = point
            require(not active, "unclosed oracle interval")
        require(self.count <= 2**63-1 and len(self.segments) <= 200_000, "oracle resource bound")

    def at(self, rank):
        require(0 <= rank < self.count, "oracle rank out of range")
        i = bisect_right(self.offsets, rank)-1
        s = self.segments[i]
        number, suffix = divmod(rank-self.offsets[i], len(s.afters))
        value = s.before + f"{s.lo+number:0{self.model.cfg.digits}d}".encode() + s.afters[suffix]
        return value, s.score

    def rank(self, value):
        parsed, score = self.model.parse(value), self.model.score(value)
        if parsed is None or not score:
            return None
        (before, after), number = parsed
        for s, offset in self.lookup.get((score, before), ()):
            if s.lo <= number < s.hi and after in s.afters:
                return offset+(number-s.lo)*len(s.afters)+s.afters.index(after)
        return None

    def export(self, path):
        # Only oracle recipe segments, never millions of individual strings.
        def token(value):
            return value.hex() or "-"
        lines = [f"RECIPE1 {self.model.cfg.digits} {len(self.segments)} {self.count}"]
        for s in self.segments:
            lines.append(" ".join([token(s.before), str(s.lo), str(s.hi), str(len(s.afters)), *map(token, s.afters)]))
        raw = ("\n".join(lines)+"\n").encode()
        require(len(raw) <= 16*1024**2, "oracle file size bound")
        with Path(path).open("xb") as stream:
            Path(path).chmod(0o600)
            stream.write(raw)
        return {"segments": len(self.segments), "candidates": self.count, "bytes": len(raw)}
