"""Read old receipt protocols as data; never execute their controllers or checkers."""
from contextlib import contextmanager
import copy
import fcntl
from pathlib import Path
import shutil
import sqlite3
import tempfile

import checkpoint_runner as journal
import coverage_snapshot as coverage
from gpu_worker import GpuMixin
from model_workflow import CORE
import runner
from runner import canonical, execute, natural, require, sha

SIMULATION = "SIMULATED-history-NO-CHECKS"
OLD_HISTORY_SCHEMA = "checkpoint-accepted-history-v1"


def input_files(root):
    result = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "migration source contains a symlink")
        if not path.is_file() or path.name.endswith(".sqlite-shm"):
            continue
        name = str(path.relative_to(root))
        coverage.safe_path(name)
        result[name] = path
    require(len(result) <= coverage.FILE_CAP, "migration source file count bound")
    require(sum(p.stat().st_size for p in result.values()) <= runner.CAP, "migration source size bound")
    return result


@contextmanager
def snapshot(source):
    """Copy a stable source, including committed WAL; no SQLite API opens it."""
    source = Path(source).resolve(strict=True)
    require(source.is_dir(), "migration source is not a directory")
    lock = None
    try:
        if (source / "owner.lock").exists():
            lock = (source / "owner.lock").open("rb")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("source campaign is owned; stop its worker before migration") from None
        with tempfile.TemporaryDirectory(prefix="coverage-source-") as temp:
            root = Path(temp) / "source"
            root.mkdir(mode=0o700)
            files = input_files(source)
            identities = {}
            for name, path in files.items():
                destination = root / name
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                identities[name] = {"sha256": sha(path), "bytes": path.stat().st_size}
                shutil.copyfile(path, destination)
                destination.chmod(0o600)
                require(sha(destination) == identities[name]["sha256"], "source changed while copying")
            yield root, identities
            require(set(input_files(source)) == set(files) and
                    all(sha(source / name) == row["sha256"] for name, row in identities.items()),
                    "source changed during migration")
    finally:
        if lock is not None:
            lock.close()


class ReceiptReader(journal.CheckpointCampaign):
    """Reuse protocol/state validation, replacing executable validation with data checks.

    Recorded source/executable hashes remain provenance. A readable receipt schema
    and its immutable plans define completed strings; old binaries need not exist.
    Only the current native plan reader is invoked, for inspection/set operations.
    """
    def core(self, *args):
        require(args and args[0] in {"describe", "inspect", "subtract"}, "migration cannot execute candidates")
        return execute([CORE, *args], file_cap=runner.PLAN_CAP if args[0] == "subtract" else runner.LOG_CAP)

    def verify_runtime(self):
        c = self.context
        require(c.get("schema") == "local-batch-runner-v1", "unsupported receipt schema")
        require(c.get("storage_schema") in {None, journal.SCHEMA, OLD_HISTORY_SCHEMA, "checkpoint-accepted-history-v2"},
                "unsupported source storage schema")
        for field in ("runner_sha256", "storage_sha256", "history_runner_sha256"):
            require(field not in c or journal.digest_string(c[field]), "invalid source software identity")
        for kind in ("core", "checker", "confirmer"):
            entry = c[kind]
            require(set(entry) == {"argv", "sha256"} and journal.digest_string(entry["sha256"]) and
                    type(entry["argv"]) is list and entry["argv"] and
                    all(type(arg) is str for arg in entry["argv"]), "invalid recorded executable identity")
        require(sha(self.blob(c["target"], "targets")) == c["target"], "source target changed")
        require(c.get("evidence_domain") in {None, "checker-receipts", SIMULATION,
                                             "SYNTHETIC-equality-rehearsal-v1", "synthetic-luks"}, "unknown source evidence domain")
        if "accepted_history" in c:
            self.accepted_base()

    def accepted_base(self):
        if "accepted_history" not in self.context:
            return None
        path = self.blob(self.context["accepted_history"], "evidence")
        require(sha(path) == self.context["accepted_history"], "accepted-history metadata changed")
        doc = coverage.read(path)
        require(set(doc) == {"schema", "kind", "policy", "target", "plan", "count", "files"} and
                doc["schema"] in {"accepted-history-snapshot-v1", "accepted-history-snapshot-v2"} and
                doc["target"] == self.context["target"],
                "unsupported source base history")
        require(natural(doc["count"]) and int(self.info(doc["plan"])["candidates"]) == doc["count"],
                "base history count mismatch")
        require(type(doc["files"]) is dict and len(doc["files"]) <= coverage.FILE_CAP + 1, "base history evidence bound")
        for digest in doc["files"].values():
            require(sha(self.blob(digest, "evidence")) == digest, "base history evidence changed")
        if doc["kind"] == "coverage":
            require(doc["schema"] == "accepted-history-snapshot-v2", "unsupported portable base history")
            files = doc["files"]
            imported = coverage.read(self.blob(files["coverage.json"], "evidence"))
            coverage.validate(imported, lambda n: self.blob(files[n], "evidence"), target=doc["target"], policy=doc["policy"])
            require(files == {"coverage.json": files["coverage.json"],
                              **{n: r["sha256"] for n, r in imported["files"].items()}} and
                    imported["completed"]["sha256"] == doc["plan"] and imported["completed"]["count"] == doc["count"],
                    "portable source base binding")
            require({"checker-receipts": "checker-receipts", "synthetic-equality": "SYNTHETIC-equality-rehearsal-v1",
                     "synthetic-luks": "synthetic-luks"}.get(imported["domain"]) == self.context["evidence_domain"],
                    "portable source base domain")
            self.coverage_hit = imported["hit"]
        elif doc["kind"] == "synthetic-fixture":
            require(self.context["evidence_domain"] == "SYNTHETIC-equality-rehearsal-v1" and
                    doc["policy"] == coverage.EQUALITY_POLICY and not doc["files"], "synthetic history policy mismatch")
            require(coverage.query(self.blob(doc["plan"]), [self.blob(doc["target"], "targets").read_bytes()]) == [False],
                    "synthetic history contains its target")
        else:
            require(doc["kind"] == "legacy-receipts" and doc["policy"] == coverage.LEGACY_POLICY and
                    self.context["evidence_domain"] == "checker-receipts", "base verifier policy mismatch")
            files = doc["files"]
            manifest = coverage.read(self.blob(files["import/manifest.json"], "evidence"))
            require(manifest["schema"] == "receipt-backed-legacy-history-v1" and
                    manifest["target_sha256"] == doc["target"] and manifest["evidence_policy"] == doc["policy"] and
                    manifest["geometry"]["sha256"] == doc["plan"] and
                    manifest["geometry"]["distinct_completed_strings"] == doc["count"], "legacy history binding")
            expected = {"import/manifest.json": files["import/manifest.json"],
                        "import/evidence.json": manifest["evidence_file_sha256"],
                        "import/duplicate-check-witnesses.json": manifest["duplicate_witness_file_sha256"],
                        **{"source/" + name: value for name, value in manifest["source_hashes"].items()}}
            require(files == expected, "source base evidence is incomplete")
        return doc

    def transition(self, doc):
        kind = doc.get("kind")
        if kind == "upgrade-v1-copy":
            require(self.state is not None and set(doc) == {"kind", "context", "origin_head", "origin_events"} and
                    doc["origin_head"] == self.head and doc["origin_events"] == self.sequence and
                    "storage_schema" not in self.context, "invalid storage-upgrade record")
            expected = dict(self.context, storage_schema=journal.SCHEMA,
                            storage_sha256=doc["context"].get("storage_sha256"), evidence_domain="checker-receipts")
            require(expected == doc["context"], "storage upgrade changed execution identities")
            self.context = copy.deepcopy(expected)
            self.verify_runtime()
            return copy.deepcopy(self.state)
        result = runner.Campaign.transition(self, doc)
        if kind == "create":
            self.full_plan = self.context.get("initial_full_plan", doc["plan"])
        elif kind == "revise":
            self.full_plan = doc["full"]
        return result


class GpuReceiptReader(GpuMixin, ReceiptReader):
    verify_runtime = ReceiptReader.verify_runtime


def read_campaign(root):
    """Verify the entire receipt chain and checkpoint reconstruction on a copy."""
    root = Path(root)
    names = [name for name in ("history.sqlite", "checkpoint.sqlite") if (root / name).is_file()]
    require(len(names) == 1, "source must contain one supported campaign store")
    # SQLite may rewrite WAL/SHM even while reading. Preserve the captured source
    # bytes as evidence and open a second, disposable database copy.
    with tempfile.TemporaryDirectory(prefix="coverage-db-") as temp:
        database = Path(temp) / names[0]
        for suffix in ("", "-wal"):
            p = root / (names[0] + suffix)
            if p.exists():
                shutil.copyfile(p, str(database) + suffix)
        db = sqlite3.connect(database)
        try:
            require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "source database damaged")
            checkpoint = None
            if names[0] == "checkpoint.sqlite":
                row = db.execute("SELECT digest,body FROM checkpoint WHERE id=1").fetchone()
                if row:
                    require(journal.sha_bytes(row[1]) == row[0], "checkpoint metadata changed")
                    checkpoint = journal.parse(row[1])
                    require(canonical(checkpoint) == row[1], "noncanonical source checkpoint")
            table = "events" if names[0] == "history.sqlite" else "tail"
            first = db.execute(f"SELECT body FROM {table} ORDER BY seq LIMIT 1").fetchone()
            context = checkpoint["context"] if checkpoint else journal.parse(first[0])["context"] if first else None
            require(context is not None, "empty campaign has no completed work")
            from gpu_backend import MARKER
            cls = GpuReceiptReader if context["checker"]["argv"][1:2] == [MARKER] else ReceiptReader
            reader = object.__new__(cls)
            reader.root, reader.info_cache = root, {}
            reader.context = reader.state = None
            reader.sequence, reader.head, reader.replayed_events = 0, runner.ZERO, 0
            reader._legacy_replay = True
            reader.verified_archive_bytes = 0
            if checkpoint:
                reader.validate_checkpoint(checkpoint)
                reader.context = None
                for descriptor in checkpoint["archives"]:
                    reader.replay_archive(reader, descriptor)
                require(reader.sequence == checkpoint["through"] and reader.head == checkpoint["head"] and
                        reader.context == checkpoint["context"] and
                        canonical(reader.state) == canonical(checkpoint["state"]),
                        "source checkpoint does not match receipt replay")
            for row in db.execute(f"SELECT seq,previous,digest,body FROM {table} ORDER BY seq"):
                reader.apply_row(row)
            require(reader.state is not None, "source contains no creation record")
            if names[0] == "checkpoint.sqlite":
                require(db.execute("SELECT seq,head FROM tip WHERE id=1").fetchone() == (reader.sequence, reader.head),
                        "source journal tip mismatch")
            reader.validate_state(reader.state)
            reader.info(reader.full_plan)
            reader.base_history = reader.accepted_base()
            return reader
        finally:
            db.close()
