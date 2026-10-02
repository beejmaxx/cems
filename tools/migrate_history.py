"""Migrate verified old receipts once; no password checking or old code execution."""
import argparse
import json
from pathlib import Path
from migrations.export import audit, export_campaign, resume
from runner import require


def inventory(root):
    root = Path(root).resolve()
    native = sorted({str(p.parent.relative_to(root)) for name in ("history.sqlite", "checkpoint.sqlite")
                     for p in root.rglob(name) if "source" not in p.relative_to(root).parts})
    legacy = []
    for p in sorted(root.rglob("manifest.json")):
        if "source" in p.relative_to(root).parts:
            continue
        try:
            if json.loads(p.read_bytes()).get("schema") == "receipt-backed-legacy-history-v1":
                legacy.append(str(p.parent.relative_to(root)))
        except (ValueError, UnicodeError):
            continue
    return {"root": str(root), "native_campaigns": native, "legacy_imports": legacy}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    c = commands.add_parser("inventory"); c.add_argument("root", type=Path)
    for name in ("export",):
        c = commands.add_parser(name)
        c.add_argument("source", type=Path); c.add_argument("destination", type=Path)
    c = commands.add_parser("audit"); c.add_argument("snapshot", type=Path)
    c = commands.add_parser("resume")
    c.add_argument("snapshot", type=Path); c.add_argument("destination", type=Path)
    c.add_argument("--accept-policy", required=True)
    c.add_argument("--plan", type=Path); c.add_argument("--target", type=Path)
    c.add_argument("--checker-json"); c.add_argument("--confirmer-json")
    a = p.parse_args()
    if a.command == "inventory":
        result = inventory(a.root)
    elif a.command == "export":
        fn = export_campaign
        doc = fn(a.source, a.destination)
        result = {"snapshot": str(a.destination), "domain": doc["domain"], "policy": doc["policy"],
                  "completed": doc["completed"]["count"], "confirmed_hit": doc["hit"] is not None, "new_checks": 0}
    elif a.command == "audit":
        result = audit(a.snapshot)
    else:
        def argv(raw):
            value = json.loads(raw) if raw else None
            require(value is None or type(value) is list and value and all(type(s) is str for s in value),
                    "commands must be JSON argument arrays")
            return value
        result = resume(a.snapshot, a.destination, plan=a.plan, target=a.target,
                        checker=argv(a.checker_json), confirmer=argv(a.confirmer_json), policy=a.accept_policy)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
