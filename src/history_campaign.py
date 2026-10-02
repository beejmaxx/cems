"""History-aware campaigns; frozen older controllers and binaries stay unchanged.

Accepted base history is copied into the campaign, bound to its target/policy,
and subtracted together with later acknowledged negatives on EVERY replan.
Candidate generation/checking still flows directly through the native pipe.
"""
from layout import BUILD, runtime_source_matches
from checker_identity import synthetic_checker_identities
import argparse
import json
from pathlib import Path
import tempfile
import time

import checkpoint_runner as journal
import coverage_snapshot as coverage
import runner as base
from model_workflow import CORE, inspect
from runner import HERE, canonical, disk_guard, natural, require, sha, sync_dir

SCHEMA = "checkpoint-accepted-history-v2"
REAL = "checker-receipts"
REHEARSAL = "SYNTHETIC-equality-rehearsal-v1"
FIXTURE_POLICY = "synthetic-equality-exclusions-NOT-real-coverage-v1"
EVIDENCE_CAP = 4 * 1024**2


def read_document(path, cap=EVIDENCE_CAP):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= cap,
            "evidence document missing or oversized")
    return journal.parse(path.read_bytes())


class HistoryCampaign(journal.CheckpointCampaign):
    @classmethod
    def create(cls, root, plan, target, checker, confirmer, *, history, history_kind,
               evidence_policy, core=CORE, checkpoint_every=journal.AUTO_EVENTS,
               geometry=coverage.GEOMETRY):
        require(history_kind in {"coverage", "synthetic-fixture"}, "explicit history kind required")
        target_sha = sha(target)
        imported = None
        if history_kind == "coverage":
            imported = coverage.load(history, target=target_sha, policy=evidence_policy, core=core)
            require(imported["domain"] != "simulation", "simulation cannot become checked coverage")
        else:
            require(evidence_policy == FIXTURE_POLICY, "history policy must be explicitly accepted")
        root = Path(root).resolve()
        root.mkdir(mode=0o700, exist_ok=False)
        for kind in ("plans", "targets", "evidence"):
            (root/kind).mkdir(mode=0o700)
        sync_dir(root.parent)
        c = cls(root, _creating=True, checkpoint_every=checkpoint_every)
        try:
            context = {"schema": "local-batch-runner-v1", "runner_sha256": sha(base.__file__),
                       "storage_schema": SCHEMA, "storage_sha256": sha(journal.__file__),
                       "history_runner_sha256": sha(__file__),
                       "core": base.pin([str(core)]), "checker": base.pin(checker),
                       "confirmer": base.pin(confirmer), "target": c.archive(target, "targets"),
                       "evidence_domain": ({"checker-receipts": REAL, "synthetic-equality": REHEARSAL,
                                            "synthetic-luks": "synthetic-luks"}[imported["domain"]]
                                           if imported is not None else REHEARSAL)}
            c.context = context
            require(context["target"] == target_sha, "target changed during creation")
            files = {}
            if imported is not None:
                history = Path(history).resolve()
                files["coverage.json"] = c.archive(history/"coverage.json", "evidence")
                require(read_document(c.blob(files["coverage.json"], "evidence")) == imported,
                        "coverage changed during snapshot")
                for name, row in imported["files"].items():
                    files[name] = c.archive(history/name, "evidence")
                    require(files[name] == row["sha256"], "coverage changed during snapshot")
                history_plan = c.archive(history/"completed.plan", "plans")
            else:
                history_plan = c.archive(history, "plans")
                secret = c.blob(context["target"], "targets").read_bytes()
                require(len(secret) <= 128, "synthetic target length")
                # For equality checking, excluding a language not containing the
                # sole true byte string is valid by construction, NOT GPU evidence.
                require(coverage.query(c.blob(history_plan), [secret], geometry=geometry) == [False],
                        "synthetic history contains the true password")
            count = int(c.info(history_plan)["candidates"])
            if imported is not None:
                require(count == imported["completed"]["count"], "coverage count mismatch")
            evidence = {"schema": "accepted-history-snapshot-v2", "kind": history_kind,
                        "policy": evidence_policy, "target": target_sha, "plan": history_plan,
                        "count": count, "files": files}
            context["accepted_history"] = c.archive_document(evidence)
            full = c.archive(plan, "plans")
            context["initial_full_plan"] = full
            c.verify_runtime()
            remaining, _ = c.prepare(full, [])
            c.append({"kind": "create", "context": context, "plan": remaining})
            return c
        except BaseException:
            c.close()
            raise

    def archive_document(self, document):
        data = canonical(document)
        require(len(data) <= EVIDENCE_CAP, "evidence metadata limit")
        disk_guard(self.root, len(data))
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=self.root) as tmp:
            path = Path(tmp)/"metadata.json"
            path.write_bytes(data)
            return self.archive(path, "evidence")

    def accepted_history(self):
        digest = self.context.get("accepted_history")
        path = self.blob(digest, "evidence")
        require(path.is_file() and sha(path) == digest, "accepted history metadata missing or changed")
        doc = read_document(path)
        require(type(doc) is dict and set(doc) == {"schema", "kind", "policy", "target", "plan", "count", "files"}
                and doc["schema"] == "accepted-history-snapshot-v2", "accepted history schema")
        domain = self.context["evidence_domain"]
        require(doc["kind"] in {"coverage", "synthetic-fixture"}, "accepted history kind")
        if doc["kind"] == "synthetic-fixture":
            require(domain == REHEARSAL and doc["policy"] == FIXTURE_POLICY,
                    "accepted history policy/domain mismatch")
        require(doc["target"] == self.context["target"], "accepted history target mismatch")
        require(natural(doc["count"]) and sha(self.blob(doc["plan"])) == doc["plan"] and
                int(self.info(doc["plan"])["candidates"]) == doc["count"], "accepted history plan missing or changed")
        require(type(doc["files"]) is dict and len(doc["files"]) <= coverage.FILE_CAP + 1, "evidence file bound")
        for digest in doc["files"].values():
            path = self.blob(digest, "evidence")
            require(path.is_file() and not path.is_symlink() and sha(path) == digest,
                    "accepted history evidence missing or changed")
        if doc["kind"] == "coverage":
            files = doc["files"]
            imported = read_document(self.blob(files.get("coverage.json"), "evidence"))
            coverage.validate(imported, lambda name: self.blob(files[name], "evidence"),
                              target=doc["target"], policy=doc["policy"])
            require(files == {"coverage.json": files["coverage.json"],
                              **{n: r["sha256"] for n, r in imported["files"].items()}},
                    "coverage evidence inventory mismatch")
            require(imported["completed"] == {"file": "completed.plan", "sha256": doc["plan"], "count": doc["count"]},
                    "coverage language mismatch")
            require({"checker-receipts": REAL, "synthetic-equality": REHEARSAL,
                     "synthetic-luks": "synthetic-luks"}.get(imported["domain"]) == domain,
                    "accepted history policy/domain mismatch")
            hit = imported["hit"]
            if hit is not None:
                row = self.core("inspect", self.blob(files["source/plans/"+hit["plan"]], "evidence"), hit["rank"], 1)
                require(row.decode().split(" ")[0] == hit["hex"], "imported hit candidate mismatch")
        else:
            require(not doc["files"], "synthetic fixture cannot claim real receipts")
        return doc

    def imported_hit(self):
        h = self.accepted_history()
        if h["kind"] == "coverage":
            return read_document(self.blob(h["files"]["coverage.json"], "evidence"))["hit"]
        return None

    def require_unresolved(self):
        require(self.imported_hit() is None, "imported history has a confirmed hit")

    def verify_runtime(self):
        base.Campaign.verify_runtime(self)
        require(self.context.get("storage_schema") == SCHEMA and
                runtime_source_matches(self.context.get("storage_sha256"), journal.__file__) and
                runtime_source_matches(self.context.get("history_runner_sha256"), __file__),
                "history-aware software changed; explicit migration required")
        require(self.context.get("evidence_domain") in {REAL, REHEARSAL, "synthetic-luks"}, "history campaign domain")
        self.accepted_history()
        full = self.context.get("initial_full_plan")
        require(sha(self.blob(full)) == full, "initial full model missing or changed")
        fixtures = synthetic_checker_identities()
        if self.context["evidence_domain"] == REHEARSAL:
            require(self.context["checker"]["sha256"] in fixtures and self.context["confirmer"]["sha256"] in fixtures and
                    self.context["checker"]["argv"][1:2] == ["check"] and
                    self.context["confirmer"]["argv"][1:] == ["confirm"], "rehearsal requires the equality fixture")
        else:
            if self.context["evidence_domain"] == "synthetic-luks":
                require(self.context["checker"]["argv"][1:3] == ["check", "control"],
                        "synthetic LUKS history requires a control checker")
            require(self.context["checker"]["sha256"] not in fixtures and self.context["confirmer"]["sha256"] not in fixtures,
                    "synthetic checker cannot create real coverage")

    def intervals(self):
        result = [(p, a, b-a) for p, ranges in sorted(self.state["ranges"].items()) for a, b in ranges]
        require(len(result) <= 512, "history interval limit; no evidence dropped")
        return result

    def subtract_to(self, full, intervals, destination):
        history = self.accepted_history()
        all_ranges = [(history["plan"], 0, history["count"]), *intervals]
        for p, _, _ in [(full, 0, 0), *all_ranges]:
            require(sha(self.blob(p)) == p, "model/history plan missing or changed")
        return json.loads(self.core("subtract", self.blob(full), destination,
                                   *[x for p, a, n in all_ranges for x in (self.blob(p), a, n)]))

    def preparation_key(self, full, remaining, intervals):
        return (self.context["accepted_history"], full, remaining, tuple(intervals))

    def prepare(self, full, intervals):
        disk_guard(self.root, 2*base.PLAN_CAP)
        with tempfile.TemporaryDirectory(prefix="prepare-history-", dir=self.root) as tmp:
            path = Path(tmp)/"remaining.plan"
            metrics = self.subtract_to(full, intervals, path)
            remaining = self.archive(path, "plans")
        self._prepared = {self.preparation_key(full, remaining, intervals)}
        return remaining, metrics

    def verify_preparation(self, full, remaining, intervals):
        key = self.preparation_key(full, remaining, intervals)
        if key not in getattr(self, "_prepared", set()):
            disk_guard(self.root, base.PLAN_CAP)
            with tempfile.TemporaryDirectory(prefix="verify-history-", dir=self.root) as tmp:
                path = Path(tmp)/"expected.plan"
                self.subtract_to(full, intervals, path)
                require(sha(path) == remaining, "revision is not full model minus accepted/completed history")
            self._prepared = {key}

    def transition(self, doc):
        kind = doc.get("kind")
        if self.state is not None:
            self.require_unresolved()
        if self.state is None and kind == "create":
            result = super().transition(doc)
            self.verify_preparation(self.context["initial_full_plan"], doc["plan"], [])
            return result
        if kind == "revise":
            result = super().transition(doc)
            self.verify_preparation(doc["full"], doc["remaining"], self.intervals())
            return result
        require(kind in {"dispatch", "complete"}, "unsupported history-aware event")
        return super().transition(doc)

    def revise(self, full_plan):
        self.usable()
        self.require_unresolved()
        self.verify_runtime()
        require(self.state["hit"] is None, "campaign has a confirmed hit")
        full = self.archive(full_plan, "plans")
        self.info(full)
        intervals = self.intervals()
        then = time.monotonic()
        remaining, metrics = self.prepare(full, intervals)
        self.append({"kind": "revise", "full": full, "remaining": remaining, "history_head": self.head})
        return {"revision": self.state["revision"], "remaining": metrics,
                "base_history_count": self.accepted_history()["count"], "local_history_intervals": len(intervals),
                "seconds": time.monotonic()-then}

    def run_batch(self, *args, **kwargs):
        self.usable()
        self.require_unresolved()
        # Both domains perform real evaluations by their pinned checker. Only
        # the explicitly synthetic domain may run the byte-equality fixture.
        return base.Campaign.run_batch(self, *args, **kwargs)

    def status(self):
        self.verify_runtime()
        result = super().status()
        history = self.accepted_history()
        result["accepted_history"] = {k: history[k] for k in ("kind", "policy", "target", "plan", "count")}
        result["imported_hit"] = self.imported_hit()
        result["checked_negative_semantics"] = "new acknowledged checks ONLY; base history is reported separately"
        return result

    def preview(self, full_plan=None, *, count=20):
        """Read-only planning: no assignment, receipt, cursor or model adoption."""
        self.usable()
        self.verify_runtime()
        require(natural(count, 100), "preview limit is 100")
        then = time.monotonic()
        if full_plan is None:
            plan = self.blob(self.state["plan"])
            start = self.state["cursor"]
            remaining = int(self.info(self.state["plan"])["candidates"])-start
            rows = inspect(Path(self.context["core"]["argv"][0]), plan, start, min(count, remaining))
        else:
            # Copy only into a disposable directory; preview does not publish a plan.
            disk_guard(self.root, base.PLAN_CAP)
            with tempfile.TemporaryDirectory(prefix="preview-history-", dir=self.root) as tmp:
                full, plan = Path(full_plan).resolve(), Path(tmp)/"remaining.plan"
                require(full.is_file() and full.stat().st_size <= base.PLAN_CAP, "preview model bound")
                h = self.accepted_history()
                intervals = [(h["plan"], 0, h["count"]), *self.intervals()]
                for p, _, _ in intervals:
                    require(sha(self.blob(p)) == p, "preview history damaged")
                info = json.loads(self.core("subtract", full, plan,
                    *[x for p, a, n in intervals for x in (self.blob(p), a, n)]))
                remaining = int(info["candidates"])
                rows = inspect(Path(self.context["core"]["argv"][0]), plan, 0, min(count, remaining))
        return {"evidence_domain": self.context["evidence_domain"], "remaining": remaining,
                "ordering": "summed model probability descending; bytewise ties",
                "candidates": [{"rank_in_next_preview": i+1, "hex": v.hex(),
                                "text": v.decode("utf-8", errors="backslashreplace"), "score": str(p)}
                               for i, (v, p) in enumerate(rows)], "seconds": time.monotonic()-then,
                "read_only": True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("create-synthetic", "create-with-coverage"):
        c = commands.add_parser(name)
        for arg in ("directory", "plan", "target", "history"):
            c.add_argument(arg, type=Path)
        if name == "create-with-coverage":
            c.add_argument("--accept-policy", required=True, help="exact policy recorded in coverage.json")
            c.add_argument("--checker-json", required=True, help="explicit checker argv JSON; never shell evaluated")
            c.add_argument("--confirmer-json", required=True)
    for name in ("status", "run", "revise", "preview", "checkpoint", "audit"):
        c = commands.add_parser(name)
        c.add_argument("directory", type=Path)
        if name in {"revise", "preview"}:
            c.add_argument("plan", type=Path, **({"nargs": "?"} if name == "preview" else {}))
        if name in {"run", "preview"}:
            c.add_argument("--count", type=int, default=1_000_000 if name == "run" else 20)
        if name == "run":
            c.add_argument("--timeout", type=float, default=100)
    args = p.parse_args()
    if args.command.startswith("create-"):
        synthetic = args.command == "create-synthetic"
        checker = str(BUILD / "synthetic-checker")
        with HistoryCampaign.create(args.directory, args.plan, args.target,
                [checker, "check", "all", "ok"] if synthetic else journal.parse(args.checker_json),
                [checker, "confirm"] if synthetic else journal.parse(args.confirmer_json),
                history=args.history, history_kind="synthetic-fixture" if synthetic else "coverage",
                evidence_policy=FIXTURE_POLICY if synthetic else args.accept_policy) as c:
            result = c.status()
    else:
        with HistoryCampaign(args.directory) as c:
            if args.command == "run":
                require(0 < args.timeout <= 3600, "timeout must be in (0, 3600]")
                result = c.run_batch(args.count, timeout=args.timeout)
            elif args.command == "revise":
                result = c.revise(args.plan)
            elif args.command == "preview":
                result = c.preview(args.plan, count=args.count)
            else:
                result = getattr(c, args.command)()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
