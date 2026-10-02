"""Read-only explanations and exact set queries for the model workshop.

The ranked plan remains authoritative. An annotated copy of the construction
supplies provenance; every autopsy must reproduce the original branch scores.
Heatmap cells mean *has an explanation using these source options in this
template*. Cells may overlap. Neither samples nor derivation counts are used as
estimates of unique candidate counts.
"""
from collections import defaultdict
from fractions import Fraction as Q
from functools import lru_cache
import json
from pathlib import Path
import time

from emitter_v1 import automaton as a
from model_workflow import CORE, METADATA_CAP, expand, explain, inspect, read_json
from plan_inspect import quoted
from recipe import compile_recipe, read_recipe, require
from runner import PLAN_CAP, execute, sha


class WorkLimit:
    def __init__(self, seconds=45, states=20_000_000):
        self.deadline = time.monotonic() + seconds
        self.remaining = states

    def tick(self):
        self.remaining -= 1
        if self.remaining < 0 or (self.remaining % 256 == 0 and time.monotonic() > self.deadline):
            raise ValueError("This view exceeded its inspection budget. Choose a smaller template or fewer heatmap rows.")


class AnnotatedNFA(a.NFA):
    """Same weighted construction semantics, with inspection-only edge events."""
    def __init__(self, roots, limit):
        self.events, self.role, self.work = {}, None, limit
        super().__init__(a.Limits(nfa_states=150_000, transitions=600_000))
        for root, weight in roots:
            start, end = self.state(), self.state()
            self.edge(self.start, None, start, weight)
            self.emit(root, start, end)
            self.edge(end, None, self.final)

    def event_edge(self, start, end, event, weight=Q(1)):
        self.events[start, len(self.edges[start])] = event
        self.edge(start, None, end, weight)

    def emit(self, expr, start, end):
        self.work.tick()
        meta = expr.get("desk")
        if meta is not None:
            role = self.role
            if "role" in meta:
                self.role = meta["role"]
            entry, leave = self.state(), self.state()
            event = dict(meta, kind="begin" if "role" in meta else "fact")
            if "source" in meta:
                event["source_role"] = self.role
            self.event_edge(start, entry, event)
            clean = {k: v for k, v in expr.items() if k != "desk"}
            self.emit(clean, entry, leave)
            self.event_edge(leave, end, {"kind": "end"}) if "role" in meta else self.edge(leave, None, end)
            self.role = role
        elif expr["op"] == "let":
            for env, p in a.contexts(expr, self.limits.finite_expansion):
                entry, leave = self.state(), self.state()
                values = {k: v["value"] for k, v in env.items() if v["op"] == "literal"}
                self.event_edge(start, entry, {"kind": "context", "values": values, "probability": str(p)}, p)
                self.emit(a.resolve(expr["output"], env), entry, leave)
                self.edge(leave, None, end)
        elif expr["op"] == "choice":
            for child, p in a.weights(expr):
                entry, leave = self.state(), self.state()
                if "desk" in child and p != 1:
                    self.event_edge(start, entry, {"kind": "choice", "choice": child["desk"], "conditional_weight": str(p)}, p)
                else:
                    self.edge(start, None, entry, p)
                self.emit(child, entry, leave)
                self.edge(leave, None, end)
        else:
            super().emit(expr, start, end)

    def trace(self, value, limit):
        @lru_cache(None)
        def visit(node, pos):
            limit.tick()
            total = best = Q(int(node == self.final and pos == len(value)))
            path = ()
            for i, (byte, dest, weight) in enumerate(self.edges[node]):
                if byte is not None and (pos == len(value) or byte != value[pos]):
                    continue
                p, b, following = visit(dest, pos + (byte is not None))
                total += weight * p
                if weight * b > best:
                    best = weight * b
                    event = self.events.get((node, i))
                    path = (((pos, event),) if event else ()) + following
            return total, best, path

        probability, best, events = visit(self.start, 0)
        pieces, contexts, current = [], [], None
        for pos, event in events:
            if event["kind"] == "begin":
                require(current is None, "nested output-role annotation")
                current = {"role": event["role"], "slot": event.get("slot"), "start": pos, "facts": []}
            elif event["kind"] == "end":
                require(current is not None, "unbalanced output-role annotation")
                current.update(end=pos, text=value[current["start"]:pos].decode("ascii"))
                pieces.append(current); current = None
            elif event["kind"] == "fact" and current is not None:
                current["facts"].append({k: v for k, v in event.items() if k not in {"kind", "source_role"}})
            elif event["kind"] == "choice" and current is not None:
                current["facts"].append({k: v for k, v in event.items() if k != "kind"})
            elif event["kind"] == "context" and event["values"]:
                contexts.append({"values": event["values"], "probability": event["probability"],
                                 "scope": current["role"] if current else "shared choices"})
        if probability:
            require("".join(p["text"] for p in pieces).encode() == value, "explanation pieces do not reconstruct candidate")
        return {"probability": str(probability), "shown_path_probability": str(best),
                "other_paths": best != probability, "pieces": pieces, "contexts": contexts}


class SupportDFA:
    """Lazily determinize an NFA after selecting latent source options."""
    def __init__(self, nfa, selections, limit):
        self.nfa, self.selections, self.work = nfa, selections, limit
        self.sets, self.ids, self.edges, self.final = [], {}, {}, []
        self._live = {}
        self.start = self.intern(self.closure({nfa.start}))

    def allowed(self, node, i):
        event = self.nfa.events.get((node, i), {})
        role = event.get("source_role")
        return role not in self.selections or event["source"] == self.selections[role]

    def live(self, node):
        if node not in self._live:
            self.work.tick()
            self._live[node] = node == self.nfa.final or any(self.allowed(node, i) and self.live(child)
                for i, (_, child, _) in enumerate(self.nfa.edges[node]))
        return self._live[node]

    def closure(self, nodes):
        seen = {n for n in nodes if self.live(n)}
        todo = list(seen)
        while todo:
            node = todo.pop(); self.work.tick()
            for i, (byte, child, _) in enumerate(self.nfa.edges[node]):
                if byte is None and child not in seen and self.allowed(node, i) and self.live(child):
                    seen.add(child); todo.append(child)
        # Epsilon-only states have no bearing on the next byte/final decision.
        return frozenset(n for n in seen if n == self.nfa.final or any(e[0] is not None for e in self.nfa.edges[n]))

    def intern(self, nodes):
        if not nodes:
            return -1
        if nodes not in self.ids:
            require(len(self.sets) < 10_000, "heatmap filter state limit")
            self.ids[nodes] = len(self.sets)
            self.sets.append(nodes); self.final.append(self.nfa.final in nodes)
        return self.ids[nodes]

    def transitions(self, state):
        if state < 0:
            return {}
        if state not in self.edges:
            following = defaultdict(set)
            for node in self.sets[state]:
                self.work.tick()
                for i, (byte, child, _) in enumerate(self.nfa.edges[node]):
                    if byte is not None and self.allowed(node, i):
                        following[byte].add(child)
            self.edges[state] = {c: self.intern(self.closure(nodes)) for c, nodes in following.items()}
        return self.edges[state]


class RankedPlan:
    """Read an already native-validated PLAB0002 plan without changing it."""
    def __init__(self, path, digest):
        path = Path(path)
        require(path.stat().st_size <= PLAN_CAP and sha(path) == digest, "inspection plan differs from its manifest")
        checked = json.loads(execute([CORE, "describe", path]))
        raw = path.read_bytes()
        require(raw[:8] == b"PLAB0002", "unsupported inspection plan")
        pos = 8

        def get(n):
            nonlocal pos
            require(pos+n <= len(raw), "truncated inspection plan")
            result = int.from_bytes(raw[pos:pos+n], "little"); pos += n
            return result

        self.denominator, nn, nb = get(16), get(4), get(4)
        require(2 <= nn <= 600_000 and nb <= 50_000 and self.denominator, "inspection plan limits")
        get(1); get(16)
        self.nodes, self.counts = [], []
        for i in range(nn):
            final, size = get(1), get(2)
            require(final in (0, 1) and size <= 256, "invalid inspection node")
            edges, last = [], -1
            for _ in range(size):
                byte, child = get(1), get(4)
                require(child < i and byte > last, "invalid inspection topology")
                edges.append((byte, child)); last = byte
            self.nodes.append((final, edges))
            self.counts.append(final + sum(self.counts[c] for _, c in edges))
        self.bands = [(get(16), get(4)) for _ in range(nb)]
        require(pos == len(raw) and all(root < nn for _, root in self.bands), "invalid inspection bands")
        self.total = sum(self.counts[root] for _, root in self.bands)
        require(self.total == int(checked["candidates"]), "inspection count differs from native plan")


    def cell(self, dfa, budget, limit):
        @lru_cache(None)
        def count(node, state):
            limit.tick()
            if state < 0:
                return 0
            final, edges = self.nodes[node]
            following = dfa.transitions(state)
            return int(final and dfa.final[state]) + sum(count(child, following[c]) for c, child in edges if following.get(c, -1) >= 0)

        def prefix(node, state, n):
            if state < 0 or n == 0:
                return 0
            if n >= self.counts[node]:
                return count(node, state)
            final, edges = self.nodes[node]
            result = int(final and dfa.final[state]); n -= final
            following = dfa.transitions(state)
            for c, child in edges:
                if n <= 0:
                    break
                take = min(n, self.counts[child])
                result += prefix(child, following.get(c, -1), take)
                n -= take
            return result

        def first(node, state):
            final, edges = self.nodes[node]
            if final and dfa.final[state]:
                return 0
            offset = final
            following = dfa.transitions(state)
            for c, child in edges:
                next_state = following.get(c, -1)
                if count(child, next_state):
                    return offset + first(child, next_state)
                offset += self.counts[child]
            raise ValueError("heatmap first rank disagrees with membership")

        total = early = offset = 0
        first_rank = None
        for _, root in self.bands:
            n = count(root, dfa.start)
            if n and first_rank is None:
                first_rank = offset + first(root, dfa.start) + 1
            total += n
            if offset < budget:
                early += prefix(root, dfa.start, min(self.counts[root], budget-offset))
            offset += self.counts[root]
        return {"count": str(total), "in_budget": str(early), "first_rank": str(first_rank) if first_rank else None}


class Views:
    def __init__(self, directory):
        self.directory = Path(directory)
        manifest = read_json(self.directory / "manifest.json")
        require(sha(CORE) == manifest["core"]["sha256"], "inspection engine changed")
        remaining = manifest.get("review", {}).get("remaining")
        self.view = "remaining" if remaining else "full"
        self.path = self.directory / ("remaining.plan" if remaining else "model.plan")
        self.digest = remaining["sha256"] if remaining else manifest["files"]["model.plan"]
        require(sha(self.directory / "recipe.toml") == manifest["review"]["recipe_sha256"], "inspection recipe changed")
        self.recipe = read_recipe(self.directory / "recipe.toml")
        require(sha(self.directory / "inputs.json") == manifest["files"]["inputs.json"], "inspection inputs changed")
        require(compile_recipe(self.recipe) == read_json(self.directory / "inputs.json"),
                "recipe interpretation changed; rebuild this draft before inspecting it")
        self.data = read_json(self.directory / "branches.json", METADATA_CAP)
        require(sha(self.directory / "branches.json") == manifest["files"]["branches.json"], "inspection branch metadata changed")
        self.branches = {b["id"]: b for b in expand(compile_recipe(self.recipe, annotate=True))}
        self.plan = None
        self.nfas, self.cache = {}, {}

    def nfa(self, key, roots, limit):
        if key not in self.nfas:
            if len(self.nfas) >= 6:
                self.nfas.pop(next(iter(self.nfas)))
            self.nfas[key] = AnnotatedNFA(roots, limit)
        return self.nfas[key]

    def autopsy(self, rank):
        require(type(rank) is int and rank > 0, "invalid candidate rank")
        require(sha(self.path) == self.digest, "inspection plan changed")
        key = ("autopsy", rank)
        if key in self.cache:
            return self.cache[key]
        limit = WorkLimit()
        rows = inspect(CORE, self.path, rank-1, 1)
        require(len(rows) == 1, "candidate rank outside this plan")
        value, score = rows[0]
        parts = explain(self.data, value)
        require(sum(Q(p["contribution"]) for p in parts) == score, "branch explanations disagree with native score")
        for part in parts:
            name = part["hypothesis"]
            trace = self.nfa(name, [(self.branches[name]["root"], Q(1))], limit).trace(value, limit)
            require(Q(trace["probability"]) == Q(part["conditional_probability"]), "annotated construction disagrees with compiled branch")
            part["trace"] = trace
            part["branch_weight"] = self.data["hypotheses"][name]["normalized_hypothesis_weight"]
            part["score_share"] = str(Q(part["contribution"])/score)
            template = next(t for t in self.recipe["templates"] if t["id"] == part["evidence"]["template"])
            family = template.get("family", "default")
            families = self.recipe.get("families", {"default": 1})
            family_mass = Q(families[family])/sum(map(Q, families.values()))
            template_mass = Q(template.get("weight", 1))/sum(Q(t.get("weight", 1)) for t in self.recipe["templates"] if t.get("family", "default") == family)
            part["selection"] = {"family": family, "family_mass": str(family_mass),
                                 "template_given_family": str(template_mass),
                                 "branch_given_template": str(Q(part["branch_weight"])/family_mass/template_mass)}
        result = {"rank": str(rank), "text": quoted(value), "hex": value.hex(), "score": str(score),
                  "view": self.view, "plan_sha256": self.digest,
                  "contributions": sorted(parts, key=lambda p: Q(p["contribution"]), reverse=True)}
        self.remember(key, result)
        return result

    def remember(self, key, result):
        if len(self.cache) >= 64:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = result


    def heatmap(self, template_id, budget, row_start=0, col_start=0):
        require(type(budget) is int and budget > 0, "invalid heatmap budget")
        require(sha(self.path) == self.digest, "inspection plan changed")
        require(type(row_start) is int and row_start >= 0 and type(col_start) is int and col_start >= 0, "invalid heatmap page")
        key = ("heatmap", template_id, budget, row_start, col_start)
        if key in self.cache:
            return self.cache[key]
        template = next((t for t in self.recipe["templates"] if t["id"] == template_id), None)
        require(template is not None and len(template.get("chunks", [])) >= 2, "select a template with at least two chunks")
        limit = WorkLimit()
        if self.plan is None:
            self.plan = RankedPlan(self.path, self.digest)
        axes = []
        for slot_name in template["chunks"][:2]:
            slot = self.recipe["slots"][slot_name]
            axes.append([None] if "alphabet" in slot else [r["value"] for r in slot["options"]])
        require(row_start < len(axes[0]) and col_start < len(axes[1]), "heatmap page outside available options")
        rows, columns = axes[0][row_start:row_start+8], axes[1][col_start:col_start+8]
        roots = [(b["root"], Q(1)) for b in self.branches.values() if b["evidence"]["template"] == template_id]
        nfa_key = "template:" + template_id
        nfa = self.nfa(nfa_key, roots, limit)
        cells = []
        for row in rows:
            for column in columns:
                dfa = SupportDFA(nfa, {"chunk1": row, "chunk2": column}, limit)
                cells.append(dict(row=row, column=column, **self.plan.cell(dfa, min(budget, self.plan.total), limit)))
        result = {"template": template_id, "rows": rows, "columns": columns, "cells": cells,
                  "row_slot": template["chunks"][0], "column_slot": template["chunks"][1],
                  "row_total": len(axes[0]), "column_total": len(axes[1]), "row_start": row_start, "col_start": col_start,
                  "budget": str(min(budget, self.plan.total)), "requested_budget": str(budget),
                  "candidates": str(self.plan.total), "view": self.view, "plan_sha256": self.digest,
                  "semantics": "unique candidates with at least one explanation using both source options in this template; cells may overlap"}
        self.remember(key, result)
        return result
