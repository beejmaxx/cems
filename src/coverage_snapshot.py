"""Portable completed-candidate sets, independent of the software that checked them."""
import json
from pathlib import Path

from layout import BUILD
from runner import CAP, PLAN_CAP, execute, natural, require, sha

SCHEMA = "completed-coverage-v1"
LEGACY_POLICY = "accept-receipt-backed-legacy-hashcat-29511-negatives-v1"
EQUALITY_POLICY = "synthetic-equality-exclusions-NOT-real-coverage-v1"
LUKS_POLICY = "luks1-master-key-digest-negatives-v1"
DOMAINS = {"checker-receipts", "synthetic-equality", "synthetic-luks", "simulation"}
GEOMETRY = BUILD / "set-geometry"
METADATA_CAP = 4 * 1024**2
FILE_CAP = 4096


def policies(policy):
    """A union retains every verifier policy; it never promotes their strength."""
    prefix = "combined-policies-v1:"
    if not policy.startswith(prefix):
        return {policy}
    from checkpoint_runner import parse
    rows = parse(policy[len(prefix):])
    require(type(rows) is list and len(rows) >= 2 and all(type(p) is str and p and
            not p.startswith(prefix) for p in rows) and rows == sorted(set(rows)), "invalid combined policies")
    return set(rows)


def combine_policies(*values):
    rows = sorted(set().union(*(policies(value) for value in values)))
    require(bool(rows), "missing verifier policy")
    return rows[0] if len(rows) == 1 else "combined-policies-v1:" + json.dumps(rows, separators=(",", ":"))


def read(path):
    # Imported lazily: checkpoint_runner itself depends on the base protocol.
    from checkpoint_runner import parse
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= METADATA_CAP,
            "coverage metadata missing or oversized")
    return parse(path.read_bytes())


def safe_path(name):
    require(type(name) is str and name and not Path(name).is_absolute() and
            ".." not in Path(name).parts and str(Path(name)) == name,
            "unsafe coverage file path")
    return name


def validate(document, resolve, *, target=None, policy=None):
    fields = {"schema", "domain", "policy", "target_sha256", "completed", "resume_plan",
              "target_file", "hit", "pending_uncredited", "origin", "files"}
    require(type(document) is dict and set(document) == fields and document["schema"] == SCHEMA,
            "unsupported completed-coverage schema")
    require(document["domain"] in DOMAINS and type(document["policy"]) is str and document["policy"],
            "coverage domain/policy missing")
    policies(document["policy"])
    from checkpoint_runner import digest_string
    require(digest_string(document["target_sha256"]), "invalid coverage target identity")
    require(target is None or target == document["target_sha256"], "coverage target mismatch")
    require(policy is None or policy == document["policy"], "coverage policy not accepted")
    files = document["files"]
    require(type(files) is dict and 0 < len(files) <= FILE_CAP, "coverage file inventory bound")
    total = 0
    for name, entry in files.items():
        safe_path(name)
        require(type(entry) is dict and set(entry) == {"sha256", "bytes"} and
                digest_string(entry["sha256"]) and natural(entry["bytes"], CAP), "invalid coverage file identity")
        path = Path(resolve(name))
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == entry["bytes"]
                and sha(path) == entry["sha256"], "coverage file missing or changed: " + name)
        total += entry["bytes"]
    require(total <= CAP, "coverage snapshot exceeds storage cap")
    completed = document["completed"]
    require(type(completed) is dict and set(completed) == {"file", "sha256", "count"} and
            completed["file"] == "completed.plan" and natural(completed["count"]) and
            files[completed["file"]]["sha256"] == completed["sha256"] and
            files[completed["file"]]["bytes"] <= PLAN_CAP, "completed language identity")
    if document["domain"] == "simulation":
        require(completed["count"] == 0 and document["hit"] is None, "simulation cannot claim completed checks")
    if document["domain"] == "synthetic-equality":
        require(document["policy"] == EQUALITY_POLICY, "synthetic equality policy mismatch")
    if document["domain"] == "synthetic-luks":
        require(document["policy"] == LUKS_POLICY, "synthetic LUKS policy mismatch")
    for name in (document["resume_plan"], document["target_file"]):
        require(name is None or name in files, "missing resume/target artifact")
    if document["target_file"] is not None:
        require(files[document["target_file"]]["sha256"] == document["target_sha256"], "target file identity")
    hit = document["hit"]
    if hit is not None:
        require(type(hit) is dict and set(hit) == {"plan", "rank", "hex"} and
                digest_string(hit["plan"]) and natural(hit["rank"]) and type(hit["hex"]) is str and
                len(hit["hex"]) <= 256 and len(hit["hex"]) % 2 == 0 and
                all(c in "0123456789abcdef" for c in hit["hex"]), "invalid imported hit")
        require("source/plans/" + hit["plan"] in files and
                files["source/plans/" + hit["plan"]]["sha256"] == hit["plan"], "imported hit plan missing or changed")
    require(type(document["origin"]) is dict, "coverage origin missing")
    return document


def load(directory, *, target=None, policy=None, core=None):
    root = Path(directory).resolve()
    document = read(root / "coverage.json")
    def resolve(name):
        path = root / safe_path(name)
        require(path.resolve().is_relative_to(root), "coverage artifact escapes snapshot")
        return path
    validate(document, resolve, target=target, policy=policy)
    if core is not None:
        info = json.loads(execute([core, "describe", root / "completed.plan"]))
        require(int(info["candidates"]) == document["completed"]["count"], "completed language count mismatch")
        if document["hit"] is not None:
            hit = document["hit"]
            row = execute([core, "inspect", root / ("source/plans/" + hit["plan"]), hit["rank"], 1])
            require(row.decode().split(" ")[0] == hit["hex"], "imported hit is not its recorded candidate")
    return document


def query(plan, values, *, geometry=GEOMETRY):
    raw = execute([geometry, "query", plan], data="".join(value.hex() + "\n" for value in values).encode())
    rows = raw.decode().splitlines()
    require(len(rows) == len(values) and all(row in {"0", "1"} for row in rows), "coverage membership result")
    return [row == "1" for row in rows]
