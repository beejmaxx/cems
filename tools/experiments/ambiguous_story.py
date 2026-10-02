"""Harder synthetic recipe: uncertain boundaries, reversal, and a copied digit.

All four models are frozen before selecting the seeded target. Useful later
clues still deliberately describe the target factory; this is not blind memory
recovery. Existing worker/checker binaries are unchanged. Explicit byte sets
are an independent bounded TEST oracle, never persistent worker history.
"""
from layout import BUILD, source as source_path
import argparse
from collections import defaultdict
from fractions import Fraction as Q
import itertools
import json
from pathlib import Path
import random
import time

from checkpoint_runner import CheckpointCampaign
from coffee_story import check_plan
from model_workflow import CORE, budget, build_bundle, explain, inspect, load_bundle, save_new
from runner import HERE, require, sha

from emitter_v1.model import choice, concat, conditional, let, literal, ref, transform, words


WHOLE = ("daybreak", "moonlight", "nightowl", "sunrise")
HEADS, TAILS = ("day", "night", "sun"), ("break", "owl", "rise")
RIGHT = ("cedar", "maple", "river", "stone", "level", "radar")
SEPARATORS, BRIDGES, ENDINGS = ("_", "-", "."), ("~", "/", ":"), ("!", "?", "")
DESCRIPTIONS = (
    "Whole-word fragments, forward spelling, independently chosen digits; lowercase favored.",
    "Add reversal of the second fragment; retain the original forward construction at higher weight.",
    "Misleading clue: only uppercase and forward spelling. Earlier evidence must survive this restriction.",
    "Restore old alternatives; add split first-fragment construction, reversed second fragment, and one digit copied twice. Favor title case.",
)


def policy(stage):
    require(stage in range(4), "unknown ambiguity stage")
    # name, weight, split-left, reverse-right, copy-number
    if stage == 2:
        return [("wrong-uppercase-forward", 1, False, False, False)], (0, 0, 1)
    branches = [("whole-forward-independent", 8 if stage == 1 else 1, False, False, False)]
    if stage >= 1:
        branches.append(("whole-reversed-independent", 1, False, True, False))
    if stage == 3:
        branches.append(("split-reversed-copied", 8, True, True, True))
    return branches, ((1, 8, 1) if stage == 3 else (8, 1, 1))


def inputs(stage):
    """The fixed clue schedule takes neither a target nor a seed."""
    def component(name):
        return {"op": "component", "name": name}

    def uniform(values):
        return choice([(str(i), literal(value), 1) for i, value in enumerate(values)])

    components = {"whole-left": uniform(WHOLE), "left-head": uniform(HEADS), "left-tail": uniform(TAILS),
        "right": uniform(RIGHT), "separator": uniform(SEPARATORS), "bridge": uniform(BRIDGES),
        "ending": uniform(ENDINGS), "digit": words("0123456789", 1)}
    components["split-left"] = concat(component("left-head"), component("left-tail"))
    branches, style_weights = policy(stage)
    style = choice([(s, literal(s), w) for s, w in zip(("lower", "title", "upper"), style_weights) if w])
    hypotheses = []
    for name, weight, split, reversed_right, copied in branches:
        left = component("split-left" if split else "whole-left")
        right = transform(component("right"), "reverse") if reversed_right else component("right")
        first = conditional("style", {"lower": left, "title": transform(left, "title"), "upper": transform(left, "upper")})
        second = conditional("style", {"lower": right, "title": right, "upper": transform(right, "upper")})
        digit = ref("n") if copied else component("digit")
        bindings = [("style", style), ("s", component("separator"))]
        if copied:
            bindings.append(("n", component("digit")))
        root = let(bindings, concat(first, ref("s"), digit, component("bridge"), second,
                                   ref("s"), digit, component("ending")))
        hypotheses.append({"id": name, "weight": weight, "root": root,
            "evidence": {"scope": "engineered clue, not real recollection", "stage": DESCRIPTIONS[stage]}})
    return {"schema": "search-constructions-v1", "description": DESCRIPTIONS[stage],
            "components": components, "hypotheses": hypotheses}


def make_secret(seed):
    rng = random.Random(seed)
    head, tail = rng.choice((("day", "break"), ("night", "owl"), ("sun", "rise")))
    word = rng.choice(("cedar", "maple", "river", "stone"))  # Not a palindrome.
    number = str(rng.randrange(10))
    separator, bridge, ending = rng.choice(SEPARATORS), rng.choice(BRIDGES), rng.choice(ENDINGS)
    value = (head+tail).title()+separator+number+bridge+word[::-1]+separator+number+ending
    return value.encode("ascii"), {"head": head, "tail": tail, "right_before_reversal": word,
        "number_chosen_once": number, "separator_chosen_once": separator, "bridge": bridge, "ending": ending}


def oracle(stage):
    """Direct recipe arithmetic; does not read emitter graphs/native plans."""
    branches, style_weights = policy(stage)
    total_branch, total_style = sum(b[1] for b in branches), sum(style_weights)
    distribution, new_support, occurrences = defaultdict(Q), set(), 0
    for _, weight, split, reversed_right, copied in branches:
        firsts = [h+t for h in HEADS for t in TAILS] if split else list(WHOLE)
        require(len(firsts) == len(set(firsts)), "test recipe unexpectedly ambiguous within one branch")
        numbers = [(str(n), str(n)) for n in range(10)] if copied else list(itertools.product("0123456789", repeat=2))
        for left, word, separator, bridge, ending, (style, sw) in itertools.product(
                firsts, RIGHT, SEPARATORS, BRIDGES, ENDINGS, zip(("lower", "title", "upper"), style_weights)):
            if not sw:
                continue
            right = word[::-1] if reversed_right else word
            if style == "title":
                left = left.title()
            elif style == "upper":
                left, right = left.upper(), right.upper()
            p = Q(weight, total_branch) * Q(sw, total_style)
            p /= len(firsts)*len(RIGHT)*len(SEPARATORS)*len(BRIDGES)*len(ENDINGS)*len(numbers)
            for n1, n2 in numbers:
                value = (left+separator+n1+bridge+right+separator+n2+ending).encode("ascii")
                distribution[value] += p
                occurrences += 1
                if split:
                    new_support.add(value)
    require(len(distribution) <= 400_000 and sum(distribution.values()) == 1, "bounded oracle mass/support")
    return dict(distribution), new_support, occurrences


def run(output, *, seed=20261001, core=CORE, checker=None):
    output, core = Path(output).resolve(), Path(core).resolve()
    checker = Path(checker or BUILD / "synthetic-checker").resolve()
    require(not output.exists(), "use a fresh experiment directory")
    budget(output, 160*1024**2)
    output.mkdir(mode=0o700)
    began = time.monotonic()
    result = {"schema": "ambiguous-story-v1", "status": "incomplete", "seed": seed,
        "scope": "engineered CPU equality experiment; no real recovery history",
        "source_sha256": sha(__file__), "inspection_helper_sha256": sha(source_path("coffee_story.py")),
        "core_sha256": sha(core), "checker_sha256": sha(checker), "stages": [], "batches": []}
    checked, delivered, done = 0, 0, set()
    try:
        # Freeze and compile the entire staged model BEFORE selecting the target.
        manifests = []
        for stage in range(4):
            budget(output, 160*1024**2)
            spec = inputs(stage)
            save_new(output / f"clues-{stage}.json", spec)
            manifests.append(build_bundle(spec, output / f"model-{stage}", core=core))
        value, recipe = make_secret(seed)
        result["password"] = value.decode()
        result["models_frozen_before_target_selection"] = True
        require(all(value.decode() not in json.dumps(inputs(s)) for s in range(4)), "full target in model")
        target = output / "FAKE-target.bin"
        with target.open("xb") as f:
            target.chmod(0o600)
            f.write(value)
        save_new(output / "ground-truth.json", {"password": value.decode(), "recipe": recipe,
            "seed": seed, "scope": "artificial factory intentionally consistent with later clues"})
        campaign = output / "campaign"
        for stage in range(4):
            distribution, new_support, occurrences = oracle(stage)
            ordered = sorted(distribution.items(), key=lambda item: (-item[1], item[0]))
            eligible = [row for row in ordered if row[0] not in done]
            bundle = output / f"model-{stage}"
            require(len(ordered) == int(manifests[stage]["native_compile"]["candidates"]), "model count mismatch")
            check_plan(core, bundle / "model.plan", ordered)
            rank = next((i for i, (v, _) in enumerate(eligible) if v == value), None)
            require((rank is None) == (stage in (0, 2)), "unexpected target support")
            row = {"stage": stage, "description": DESCRIPTIONS[stage], "model_candidates": len(ordered),
                "derivation_occurrences": occurrences, "eligible_candidates": len(eligible),
                "target_eligible_rank_zero_based": rank, "checked_before": len(done),
                "completed_overlaps_excluded": len(ordered)-len(eligible),
                "all_full_model_bytes_and_scores_verified": True}
            result["stages"].append(row)
            if not stage:
                with CheckpointCampaign.create(campaign, bundle / "model.plan", target,
                    [str(checker), "check", "4096", "ok"], [str(checker), "confirm"], core=core, checkpoint_every=0):
                    pass
            with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                if stage:
                    row["revision"] = c.revise(bundle / "model.plan")
                require(c.status()["available_in_plan"] == len(eligible), "history subtraction count mismatch")
                check_plan(core, c.blob(c.state["plan"]), eligible)
                row["all_eligible_bytes_and_scores_verified"] = True
                require(c.state["checked_negative"] == len(done), "restricted model erased evidence")
            if stage == 3:
                require(row["completed_overlaps_excluded"] == len(done), "restoring support lost old evidence")
                witnesses = sorted(done & new_support)[:3]
                require(witnesses, "no completed overlap through newly added boundaries/copying")
                _, compiled = load_bundle(bundle)
                row["new_rule_reproduces_but_does_not_recheck"] = [{"text": v.decode(),
                    "contributions": explain(compiled, v)} for v in witnesses]
                require(all(any(c["hypothesis"] == "split-reversed-copied" for c in w["contributions"])
                            for w in row["new_rule_reproduces_but_does_not_recheck"]), "new overlap not reproduced")
            # Fixed pre-final budgets, never selected from the secret's rank.
            for attempt in range((2, 2, 1, 100)[stage]):
                with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                    start = c.state["cursor"]
                    submitted = eligible[start:start+6144]
                    require(submitted and not (set(v for v, _ in submitted) & done), "acknowledged guess repeated")
                    answer = c.run_batch(len(submitted))
                    k = answer["committed_negative"]
                    negatives = {v for v, _ in submitted[:k]}
                    require(len(negatives) == k and not (negatives & done) and value not in negatives,
                            "duplicate or false-negative coverage")
                    done.update(negatives)
                    checked += k + answer["confirmed_hits"]
                    delivered += answer["generated_and_delivered"]
                    require(c.state["checked_negative"] == len(done), "durable history count mismatch")
                    if answer["status"] == "hit":
                        require(submitted[k][0] == value and c.state["hit"]["hex"] == value.hex(), "incorrect hit")
                    elif answer["uncredited_tail"]:
                        require(inspect(core, c.blob(c.state["plan"]), c.state["cursor"], 1)[0] == submitted[k],
                                "unchecked tail lost")
                    result["batches"].append({"stage": stage, **answer})
                    row["last_status"] = c.status()
                with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                    require(c.state["cursor"] == start+k and c.state["pending"] is None, "restart mismatch")
                if answer["status"] == "hit":
                    break
            require((row["last_status"]["hit"] is not None) == (stage == 3), "unexpected recovery stage")
            row["checked_after"] = len(done)
            with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                row["checkpoint"] = c.checkpoint()
            save_new(output / f"stage-{stage}.json", row)
            print(json.dumps({"stage": stage, "rank": rank, "checked": len(done),
                "excluded": row["completed_overlaps_excluded"], "found": stage == 3}), flush=True)
        with CheckpointCampaign(campaign, checkpoint_every=0) as c:
            result["audit"], result["final_status"] = c.audit(), c.status()
            require(result["audit"]["verified"] and c.state["hit"]["hex"] == value.hex(), "final audit/hit failed")
        require(result["stages"][3]["target_eligible_rank_zero_based"] < result["stages"][1]["target_eligible_rank_zero_based"],
                "new dependency/boundary clue did not promote target")
        result.update(status="passed", actual_equality_checks=checked, delivered_frames=delivered,
            acknowledged_distinct_negatives=len(done), repeated_acknowledged_negatives=0,
            separate_confirmation_checks=1, restarts=len(result["batches"]), explicit_sets_are_test_only=True)
    except BaseException as error:
        result["failure"] = repr(error)
        raise
    finally:
        result["seconds"] = time.monotonic()-began
        result["storage_before_results"] = budget(output)
        save_new(output / "results.json", result)
    lines = ["# Ambiguous construction campaign", "", f"Synthetic target: `{value.decode()}`.",
        "All four clue/model versions were frozen before selecting the seeded target. The factory intentionally follows the later construction class; this is not blind memory recovery.", "",
        "| Version | Distinct candidates | Raw derivations | Target eligible rank (zero-based) | Prior negatives excluded |",
        "| --- | ---: | ---: | ---: | ---: |"]
    for row in result["stages"]:
        rank = row["target_eligible_rank_zero_based"]
        lines.append(f"| {row['stage']}: {row['description']} | {row['model_candidates']:,} | {row['derivation_occurrences']:,} | {rank if rank is not None else 'not admitted'} | {row['completed_overlaps_excluded']:,} |")
    lines += ["", f"Recovered after **{checked:,} actual equality checks**, plus one separate confirmation. Zero repeated acknowledged negatives.",
        "Every byte and exact score in all full models and all history-subtracted models matched the independent bounded recipe oracle.",
        "New split/reversed/copied constructions were explicitly shown to reproduce already checked strings. They remained excluded.",
        "The misleading version did not admit the target; restoring support did not erase historical evidence. Restart and receipt audit passed.",
        "The exact-number choice is charged once when copied, versus twice when independently chosen. Palindromic fragments deliberately overlap forward and reversed explanations.",
        "No candidate wordlist or real recovery coverage was written. Explicit sets are bounded test-oracle memory, not the unchanged worker's persistent history.",
        f"Full correctness-experiment time: {result['seconds']:.3f} seconds, including exhaustive Python oracle work; not a throughput benchmark.",
        f"Artifacts before results/report: {result['storage_before_results']['retained_bytes']:,} bytes.", ""]
    with (output / "REPORT.md").open("x") as f:
        (output / "REPORT.md").chmod(0o600)
        f.write("\n".join(lines))
    budget(output)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new_directory", type=Path)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--core", type=Path, default=CORE)
    parser.add_argument("--checker", type=Path, default=BUILD / "synthetic-checker")
    args = parser.parse_args()
    run(args.new_directory, seed=args.seed, core=args.core, checker=args.checker)
