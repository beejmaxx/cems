"""One-time conversion of acknowledged history into portable exact coverage."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile

from checker_identity import synthetic_checker_identities
import coverage_snapshot as coverage
from model_workflow import CORE
from runner import CAP, RESERVE, PLAN_CAP, canonical, disk_guard, execute, require, sha, sync_dir
from migrations.reader import SIMULATION, input_files, read_campaign, snapshot


def native(*args):
    return execute([coverage.GEOMETRY, *args], file_cap=PLAN_CAP)


def count(plan):
    return int(json.loads(execute([CORE, "describe", plan]))["candidates"])


def classify(c):
    ctx = c.context
    if ctx.get("evidence_domain") == SIMULATION:
        return "simulation", "no-checks-no-credit-v1"
    if ctx.get("evidence_domain") == "SYNTHETIC-equality-rehearsal-v1":
        require(ctx["checker"]["sha256"] in synthetic_checker_identities(),
                "unknown synthetic checker cannot be promoted to real coverage")
    if ctx["checker"]["sha256"] in synthetic_checker_identities():
        return "synthetic-equality", coverage.EQUALITY_POLICY
    if ctx["checker"]["argv"][1:3] == ["check", "control"]:
        return "synthetic-luks", coverage.LUKS_POLICY
    from gpu_backend import MARKER, POLICY
    policy = POLICY if ctx["checker"]["argv"][1:2] == [MARKER] else "pinned-checker-sha256:" + ctx["checker"]["sha256"]
    if c.base_history is not None and c.base_history["count"]:
        policies = {c.base_history["policy"]}
        if c.state["checked_negative"]:
            policies.add(policy)
        policy = coverage.combine_policies(*policies)
    return "checker-receipts", policy


def completed_plan(c, destination):
    """Intervals are ranks in their OWN immutable plans, never in a new model."""
    if classify(c)[0] == "simulation":
        native("select", c.blob(c.full_plan), 0, 0, destination)
        return
    with tempfile.TemporaryDirectory(prefix="coverage-union-") as tmp:
        root, plans = Path(tmp), []
        if c.base_history is not None:
            plans.append(c.blob(c.base_history["plan"]))
        for digest, ranges in sorted(c.state["ranges"].items()):
            for a, b in ranges:
                disk_guard(root, PLAN_CAP)
                part = root / (str(len(plans)) + ".plan")
                native("select", c.blob(digest), a, b-a, part)
                plans.append(part)
        if not plans:
            native("select", c.blob(c.full_plan), 0, 0, destination)
        else:
            level = 0
            while len(plans) > 64:
                next_level = []
                for i in range(0, len(plans), 64):
                    disk_guard(root, PLAN_CAP)
                    part = root / f"union-{level}-{i}.plan"
                    native("union", part, *plans[i:i+64])
                    next_level.append(part)
                plans, level = next_level, level+1
            native("union", destination, *plans)


def native_document(c, root):
    domain, policy = classify(c)
    hit = None if domain == "simulation" else c.state["hit"] or getattr(c, "coverage_hit", None)
    if hit is not None and not c.blob(hit["plan"]).exists():
        # A second migration must retain the original hit's coordinate system.
        original = c.blob(c.base_history["files"]["source/plans/" + hit["plan"]], "evidence")
        destination = root / "source/plans" / hit["plan"]
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
    completed_plan(c, root / "completed.plan")
    shutil.copyfile(c.blob(c.full_plan), root / "resume.plan")
    shutil.copyfile(c.blob(c.context["target"], "targets"), root / "target.bin")
    return {"schema": coverage.SCHEMA, "domain": domain, "policy": policy,
            "target_sha256": c.context["target"],
            "completed": {"file": "completed.plan", "sha256": sha(root / "completed.plan"),
                          "count": count(root / "completed.plan")},
            "resume_plan": "resume.plan", "target_file": "target.bin",
            "hit": hit,
            "pending_uncredited": c.state["pending"],
            "origin": {"format": "native-campaign", "receipt_head": c.head,
                       "events": c.sequence, "context": c.context,
                       "reported_negative_checks": c.state["checked_negative"],
                       "base_count": c.base_history["count"] if c.base_history else 0}, "files": {}}


def finish(root, doc):
    files = input_files(root)
    for path in files.values():
        path.chmod(0o600)
    doc["files"] = {name: {"sha256": sha(p), "bytes": p.stat().st_size} for name, p in files.items()}
    (root / "coverage.json").write_bytes(canonical(doc))
    (root / "coverage.json").chmod(0o600)
    coverage.load(root, core=CORE)
    for p in root.rglob("*"):
        if p.is_file():
            with p.open("rb") as f:
                os.fsync(f.fileno())
        elif p.is_dir():
            p.chmod(0o700)
    sync_dir(root)
    return doc


@contextmanager
def publication(destination):
    destination = Path(destination).resolve()
    require(not destination.exists(), "migration destination already exists")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(shutil.disk_usage(destination.parent).free >= RESERVE + 3*CAP, "migration disk reserve")
    with tempfile.TemporaryDirectory(prefix=".migrating-", dir=destination.parent) as tmp:
        root = Path(tmp) / "snapshot"
        root.mkdir(mode=0o700)
        yield root
        require(not destination.exists(), "migration destination already exists")
        root.rename(destination)
        sync_dir(destination.parent)


def export_campaign(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    require(not destination.is_relative_to(source), "destination must be outside the source campaign")
    with publication(destination) as root:
        with snapshot(source) as (captured, _):
            c = read_campaign(captured)
            # A discovery outside the journal is important but has no negative
            # coverage authority. Refuse automatic conversion until reconciled.
            require(not (captured / "discoveries").exists() or c.state["hit"] is not None,
                    "uncommitted discovery requires reconciliation before migration")
            shutil.copytree(captured, root / "source")
            doc = finish(root, native_document(c, root))
        # The source snapshot context verifies unchanged bytes before publishing.
        audit(root)
    return doc




def audit(directory):
    """Reproduce completed coverage using only the exported evidence bytes."""
    root = Path(directory).resolve()
    doc = coverage.load(root, core=CORE)
    if doc["origin"]["format"] == "native-campaign":
        c = read_campaign(root / "source")
        require(sha(root / "resume.plan") == c.full_plan, "resume plan differs from source model")
        with tempfile.TemporaryDirectory(prefix="coverage-audit-") as tmp:
            rebuilt = native_document(c, Path(tmp))
        require({k: v for k, v in doc.items() if k != "files"} ==
                {k: v for k, v in rebuilt.items() if k != "files"}, "migration differs from receipt replay")
    elif doc["origin"]["format"] == "legacy-import":
        raise RuntimeError("This historical adapter must be supplied privately.")
    else:
        raise RuntimeError("unsupported coverage origin")
    return {"verified": True, "domain": doc["domain"], "completed": doc["completed"]["count"],
            "confirmed_hit": doc["hit"] is not None, "new_checks": 0}


def resume(snapshot_path, destination, *, plan=None, target=None, checker=None, confirmer=None, policy):
    """Create idle current-format state; importing never invokes a checker."""
    from history_campaign import HistoryCampaign
    root = Path(snapshot_path).resolve()
    doc = coverage.load(root, policy=policy, core=CORE)
    require(doc["domain"] != "simulation", "simulation cannot become checked coverage")
    audit(root)
    ctx = doc["origin"].get("context", {})
    plan = plan or (root / doc["resume_plan"] if doc["resume_plan"] else None)
    target = target or (root / doc["target_file"] if doc["target_file"] else None)
    require(plan is not None and target is not None, "an explicit current plan and matching target are required")
    if checker is None and ctx.get("checker"):
        checker = ctx["checker"]["argv"]
        require(sha(checker[0]) == ctx["checker"]["sha256"], "old checker changed; select a current checker explicitly")
    if confirmer is None and ctx.get("confirmer"):
        confirmer = ctx["confirmer"]["argv"]
        require(sha(confirmer[0]) == ctx["confirmer"]["sha256"], "old confirmer changed; select a current confirmer explicitly")
    require(checker and confirmer, "explicit checker and confirmer commands are required")
    from gpu_backend import MARKER
    require(checker[1:2] != [MARKER], "use gpu create with a current qualification and this coverage snapshot")
    with HistoryCampaign.create(destination, plan, target, checker, confirmer, core=CORE,
            history=root, history_kind="coverage", evidence_policy=policy) as c:
        c.checkpoint()
        require(c.audit()["verified"], "new campaign audit failed")
        return {"directory": str(Path(destination).resolve()), "imported": doc["completed"]["count"],
                "domain": doc["domain"], "confirmed_hit": doc["hit"] is not None, "new_checks": 0}
