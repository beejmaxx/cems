"""Checkpointed storage for the SAME native worker. No per-candidate changes.

The v1 runner is frozen for existing evidence. This subclass changes batch
journaling, not enumeration, checker semantics, probability order or coverage.
"""
from layout import BUILD, runtime_source_matches
import argparse
import copy
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

import runner as v1
from runner import canonical, disk_guard, natural, require, sha, sync_dir

AUTO_EVENTS = 4096
TAIL_CAP = 100_000
CHECKPOINT_CAP = 4 * 1024**2
ARCHIVE_CAP = 64 * 1024**2
RAW_ARCHIVE_CAP = 128 * 1024**2
ARCHIVE_COUNT_CAP = 2048
SCHEMA = "checkpoint-journal-v1"
DOMAINS = {"checker-receipts", "SIMULATED-history-NO-CHECKS"}


def parse(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, "duplicate JSON key")
            out[key] = value
        return out
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: require(False, "nonfinite JSON"))


def digest_string(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


class CheckpointCampaign(v1.Campaign):
    def __init__(self, root, *, _creating=False, checkpoint_every=AUTO_EVENTS):
        require(natural(checkpoint_every, TAIL_CAP), "invalid checkpoint interval")
        self.root = Path(root).resolve()
        require(self.root.is_dir(), "campaign missing; resume never creates history")
        self.lock = self.db = self.state = self.context = None
        self.info_cache = {}
        self.head, self.sequence = v1.ZERO, 0
        self.boundary = None
        self.through = self.tail_count = 0
        self.archives = []
        self.checkpoint_every = checkpoint_every
        self.poisoned = False
        self._legacy_replay = False
        self.replayed_events = 0
        self.verified_archive_bytes = 0
        try:
            lock_path = self.root / "owner.lock"
            require(_creating or lock_path.is_file(), "owner lock missing")
            self.lock = lock_path.open("a+b")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("campaign already has an owner") from None
            database = self.root / "checkpoint.sqlite"
            require(_creating or database.is_file(), "checkpoint database missing; use explicit import-copy for v1")
            if _creating:
                (self.root / "archives").mkdir(mode=0o700)
            require((self.root / "archives").is_dir(), "audit archive directory missing")
            self.db = sqlite3.connect(database, isolation_level=None)
            if _creating:
                self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA fullfsync=ON")
            self.db.execute("PRAGMA checkpoint_fullfsync=ON")
            self.db.execute("PRAGMA trusted_schema=OFF")
            if _creating:
                self.db.execute("CREATE TABLE tail(seq INTEGER PRIMARY KEY, previous TEXT NOT NULL, digest TEXT NOT NULL, body BLOB NOT NULL)")
                self.db.execute("CREATE TABLE checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL, body BLOB NOT NULL)")
                self.db.execute("CREATE TABLE tip(id INTEGER PRIMARY KEY CHECK(id=1), seq INTEGER NOT NULL, head TEXT NOT NULL)")
                self.db.execute("INSERT INTO tip VALUES(1,0,?)", (v1.ZERO,))
                sync_dir(self.root)
            else:
                require(self.db.execute("PRAGMA quick_check").fetchone() == ("ok",), "database integrity failed")
                entry = self.db.execute("SELECT digest,body FROM checkpoint WHERE id=1").fetchone()
                if entry is not None:
                    d, raw = entry
                    require(len(raw) <= CHECKPOINT_CAP and sha_bytes(raw) == d, "checkpoint damaged")
                    cp = parse(raw)
                    require(canonical(cp) == raw, "checkpoint noncanonical")
                    self.validate_checkpoint(cp)
                    self.boundary = cp
                    self.through, self.sequence, self.head = cp["through"], cp["through"], cp["head"]
                    self.context, self.state = copy.deepcopy(cp["context"]), self.decode_state(cp["state"])
                    self.archives = cp["archives"]
                for row in self.db.execute("SELECT seq,previous,digest,body FROM tail ORDER BY seq"):
                    self.apply_row(row)
                    self.tail_count += 1
                    require(self.tail_count <= TAIL_CAP, "active journal limit")
                require(self.state is not None, "empty history is not a resumable campaign")
                require(self.db.execute("SELECT seq,head FROM tip WHERE id=1").fetchone() == (self.sequence, self.head),
                        "journal tip mismatch; history may be missing")
                self.verify_runtime()
                self.validate_state(self.state)
        except BaseException:
            self.close()
            raise

    @classmethod
    def create(cls, root, plan, target, checker, confirmer, *, core=None,
               checkpoint_every=AUTO_EVENTS, evidence_domain="checker-receipts"):
        require(evidence_domain in DOMAINS, "invalid evidence domain")
        root = Path(root).resolve()
        root.mkdir(mode=0o700, exist_ok=False)
        (root / "plans").mkdir(mode=0o700)
        (root / "targets").mkdir(mode=0o700)
        sync_dir(root.parent)
        c = cls(root, _creating=True, checkpoint_every=checkpoint_every)
        try:
            context = {"schema": "local-batch-runner-v1", "runner_sha256": sha(v1.__file__),
                       "core": v1.pin([str(core or BUILD / "prep")]), "checker": v1.pin(checker),
                       "confirmer": v1.pin(confirmer), "target": c.archive(target, "targets"),
                       "storage_schema": SCHEMA, "storage_sha256": sha(__file__), "evidence_domain": evidence_domain}
            c.context = context
            p = c.archive(plan, "plans")
            c.append({"kind": "create", "context": context, "plan": p})
            return c
        except BaseException:
            c.close()
            raise

    def usable(self):
        require(not self.poisoned, "checkpoint operation failed; close and reopen the campaign")

    def verify_runtime(self):
        super().verify_runtime()
        if self._legacy_replay and "storage_schema" not in self.context:
            return
        require(self.context.get("storage_schema") == SCHEMA and runtime_source_matches(self.context.get("storage_sha256"), __file__),
                "checkpoint engine changed; explicit migration required")
        require(self.context.get("evidence_domain") in DOMAINS, "unknown evidence domain")

    def transition(self, doc):
        if doc.get("kind") == "upgrade-v1-copy":
            require(self._legacy_replay and self.state is not None and "storage_schema" not in self.context,
                    "unexpected v1 upgrade event")
            require(set(doc) == {"kind", "context", "origin_head", "origin_events"} and
                    doc["origin_head"] == self.head and doc["origin_events"] == self.sequence, "upgrade origin mismatch")
            expected = dict(self.context, storage_schema=SCHEMA, storage_sha256=doc["context"].get("storage_sha256"), evidence_domain="checker-receipts")
            require(runtime_source_matches(expected["storage_sha256"], __file__), "upgrade storage identity changed")
            require(doc["context"] == expected, "upgrade changed target/checker/generator identity")
            self.context = copy.deepcopy(expected)
            self._legacy_replay = False
            self.verify_runtime()
            return self.state.copy()
        return super().transition(doc)

    def apply_row(self, row):
        seq, previous, d, raw = row
        require(natural(seq) and seq == self.sequence + 1 and previous == self.head and
                type(raw) is bytes and len(raw) <= v1.LOG_CAP and sha_bytes(previous.encode() + raw) == d,
                "receipt chain damaged")
        doc = parse(raw)
        require(canonical(doc) == raw, "noncanonical receipt")
        self.state = self.transition(doc)
        self.sequence, self.head = seq, d
        self.replayed_events += 1

    def append(self, doc):
        self.usable()
        # Compact before reaching the old cap, even with automatic maintenance disabled.
        if self.tail_count >= TAIL_CAP:
            self.checkpoint()
        require(self.sequence < v1.MAX_COORD, "journal sequence limit")
        next_state = self.transition(doc)
        body = canonical(doc)
        require(len(body) <= v1.LOG_CAP, "event size limit")
        d = sha_bytes(self.head.encode() + body)
        disk_guard(self.root, 2 * 1024**2)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            require(self.db.execute("SELECT seq,head FROM tip WHERE id=1").fetchone() == (self.sequence, self.head), "journal changed")
            self.db.execute("INSERT INTO tail VALUES(?,?,?,?)", (self.sequence + 1, self.head, d, body))
            self.db.execute("UPDATE tip SET seq=?,head=? WHERE id=1", (self.sequence + 1, d))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        self.sequence += 1
        self.tail_count += 1
        self.head, self.state = d, next_state
        if self.checkpoint_every and self.tail_count >= self.checkpoint_every:
            self.checkpoint()

    @staticmethod
    def decode_state(state):
        result = copy.deepcopy(state)
        result["ranges"] = {p: [tuple(r) for r in ranges] for p, ranges in result["ranges"].items()}
        return result

    def validate_state(self, state):
        require(type(state) is dict and set(state) == {"revision", "plan", "cursor", "pending", "hit", "ranges", "checked_negative", "receipts"},
                "checkpoint state fields")
        require(natural(state["revision"]) and natural(state["receipts"]) and natural(state["checked_negative"]) and
                natural(state["cursor"], int(self.info(state["plan"])["candidates"])), "checkpoint counters")
        require(type(state["ranges"]) is dict and sum(len(x) for x in state["ranges"].values()) <= 512, "checkpoint interval limit")
        total = 0
        for p, intervals in state["ranges"].items():
            require(type(intervals) is list and intervals, "empty/invalid checkpoint intervals")
            require(sha(self.blob(p)) == p, "coverage plan damaged")
            count = int(self.info(p)["candidates"])
            previous = -1
            for interval in intervals:
                require(type(interval) in (list, tuple) and len(interval) == 2, "interval shape")
                a, b = interval
                require(natural(a, count) and natural(b, count) and previous < a < b, "noncanonical checkpoint intervals")
                previous = b
                total += b - a
        require(total == state["checked_negative"], "checkpoint coverage count mismatch")
        if state["pending"] is not None:
            old_state = self.state
            try:
                self.state = dict(state, pending=None)
                super().transition({"kind": "dispatch", "job": state["pending"]})
            finally:
                self.state = old_state
        if state["hit"] is not None:
            hit = state["hit"]
            require(state["pending"] is None and type(hit) is dict and set(hit) == {"plan", "rank", "hex"} and
                    hit["plan"] == state["plan"] and hit["rank"] == state["cursor"] and
                    natural(hit["rank"]) and hit["rank"] < int(self.info(hit["plan"])["candidates"]), "checkpoint hit position")
            expected = self.core("inspect", self.blob(hit["plan"]), hit["rank"], 1).decode().split(" ")[0]
            require(expected == hit["hex"], "checkpoint hit bytes")
        require(sha(self.blob(state["plan"])) == state["plan"], "active plan damaged")

    def archive_path(self, d):
        require(digest_string(d), "invalid archive digest")
        return self.root / "archives" / (d + ".gz")

    def validate_checkpoint(self, cp):
        require(type(cp) is dict and set(cp) == {"schema", "through", "head", "context", "state", "archives"} and
                cp["schema"] == SCHEMA and natural(cp["through"]) and cp["through"] > 0 and digest_string(cp["head"]),
                "invalid checkpoint header")
        self.context = copy.deepcopy(cp["context"])
        self.verify_runtime()
        self.validate_state(cp["state"])
        descriptors = cp["archives"]
        require(type(descriptors) is list and 1 <= len(descriptors) <= ARCHIVE_COUNT_CAP, "audit archive count limit")
        sequence, head = 0, v1.ZERO
        for a in descriptors:
            require(type(a) is dict and set(a) == {"sha256", "bytes", "raw_bytes", "first", "last", "previous", "head"} and
                    a["first"] == sequence + 1 and natural(a["last"]) and a["last"] >= a["first"] and
                    a["previous"] == head and digest_string(a["head"]) and natural(a["bytes"], ARCHIVE_CAP) and
                    natural(a["raw_bytes"], RAW_ARCHIVE_CAP), "audit archive chain invalid")
            path = self.archive_path(a["sha256"])
            require(path.is_file() and not path.is_symlink() and path.stat().st_size == a["bytes"] and sha(path) == a["sha256"],
                    "audit archive missing or damaged")
            self.verified_archive_bytes += a["bytes"]
            sequence, head = a["last"], a["head"]
        require(sequence == cp["through"] and head == cp["head"], "checkpoint/archive boundary mismatch")

    def replay_object(self, boundary=None, *, legacy=False):
        other = object.__new__(type(self))
        other.root, other.info_cache = self.root, {}
        other.context = copy.deepcopy(boundary["context"]) if boundary else None
        other.state = self.decode_state(boundary["state"]) if boundary else None
        other.sequence = boundary["through"] if boundary else 0
        other.head = boundary["head"] if boundary else v1.ZERO
        other._legacy_replay = legacy
        other.replayed_events = 0
        return other

    def replay_archive(self, other, descriptor):
        raw_bytes = 0
        first = other.sequence + 1
        with gzip.open(self.archive_path(descriptor["sha256"]), "rb") as stream:
            for line in iter(lambda: stream.readline(2 * v1.LOG_CAP + 1024), b""):
                raw_bytes += len(line)
                require(len(line) <= 2 * v1.LOG_CAP and raw_bytes <= RAW_ARCHIVE_CAP and line.endswith(b"\n"), "archive decode bound")
                record = parse(line)
                require(type(record) is list and len(record) == 4 and type(record[3]) is str, "archive record shape")
                other.apply_row((*record[:3], record[3].encode()))
        require(raw_bytes == descriptor["raw_bytes"] and first == descriptor["first"] and
                other.sequence == descriptor["last"] and other.head == descriptor["head"], "archive contents/boundary mismatch")

    def audit(self):
        """Explicit full replay of every retained receipt; normal resume does not do this."""
        self.usable()
        began = time.monotonic()
        other = self.replay_object(legacy=True)
        for a in self.archives:
            require(sha(self.archive_path(a["sha256"])) == a["sha256"], "audit archive changed")
            self.replay_archive(other, a)
        if self.boundary:
            require(canonical(other.state) == canonical(self.boundary["state"]) and other.context == self.boundary["context"],
                    "checkpoint does not match receipt replay")
        for row in self.db.execute("SELECT seq,previous,digest,body FROM tail ORDER BY seq"):
            other.apply_row(row)
        require(other.sequence == self.sequence and other.head == self.head and other.context == self.context and
                canonical(other.state) == canonical(self.state), "live state does not match receipt replay")
        return {"verified": True, "events_replayed": other.replayed_events, "seconds": time.monotonic() - began}

    def checkpoint(self, *, fault=None):
        self.usable()
        require(fault in {None, "after_archive", "before_commit", "after_commit"}, "invalid checkpoint fault")
        if not self.tail_count:
            return {"status": "already_checkpointed", "through": self.through}
        require(len(self.archives) < ARCHIVE_COUNT_CAP, "audit archive count limit; no history dropped")
        # Compaction may dirty many SQLite pages before a WAL checkpoint.
        disk_guard(self.root, ARCHIVE_CAP + (self.root / "checkpoint.sqlite").stat().st_size + 8 * 1024**2)
        began = time.monotonic()
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=self.root / "archives")
        temporary = Path(name)
        try:
            raw_size = 0
            with os.fdopen(fd, "wb") as file:
                with gzip.GzipFile(fileobj=file, mode="wb", filename="", mtime=0, compresslevel=6) as compressed:
                    for seq, previous, d, body in self.db.execute("SELECT seq,previous,digest,body FROM tail ORDER BY seq"):
                        line = canonical([seq, previous, d, body.decode()]) + b"\n"
                        require(len(line) <= 2 * v1.LOG_CAP, "archive record bound")
                        raw_size += len(line)
                        require(raw_size <= RAW_ARCHIVE_CAP and file.tell() <= ARCHIVE_CAP - v1.LOG_CAP, "archive resource bound")
                        compressed.write(line)
                file.flush()
                os.fsync(file.fileno())
            require(temporary.stat().st_size <= ARCHIVE_CAP, "compressed archive bound")
            d = sha(temporary)
            destination = self.archive_path(d)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                require(sha(destination) == d, "existing archive damaged")
            sync_dir(destination.parent)
            a = {"sha256": d, "bytes": destination.stat().st_size, "raw_bytes": raw_size,
                 "first": self.through + 1, "last": self.sequence,
                 "previous": self.boundary["head"] if self.boundary else v1.ZERO, "head": self.head}
            # Replay the *published archive* against the previous trusted boundary.
            # Comparing with memory alone would allow silently compacting false coverage.
            other = self.replay_object(self.boundary, legacy=True)
            self.replay_archive(other, a)
            require(other.sequence == self.sequence and other.head == self.head and other.context == self.context and
                    canonical(other.state) == canonical(self.state), "checkpoint replay disagrees with active state")
            self.verify_runtime()
            self.validate_state(other.state)
            cp = {"schema": SCHEMA, "through": self.sequence, "head": self.head,
                  "context": self.context, "state": other.state, "archives": [*self.archives, a]}
            body = canonical(cp)
            require(len(body) <= CHECKPOINT_CAP, "checkpoint metadata bound")
            if fault == "after_archive":
                raise RuntimeError("injected interruption after archive publication")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                require(self.db.execute("SELECT seq,head FROM tip WHERE id=1").fetchone() == (self.sequence, self.head), "journal changed")
                self.db.execute("INSERT OR REPLACE INTO checkpoint VALUES(1,?,?)", (sha_bytes(body), body))
                self.db.execute("DELETE FROM tail WHERE seq<=?", (self.sequence,))
                if fault == "before_commit":
                    raise RuntimeError("injected interruption before checkpoint commit")
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
            if fault == "after_commit":
                raise RuntimeError("injected interruption after checkpoint commit")
            compacted = self.tail_count
            self.boundary, self.through, self.tail_count, self.archives = cp, self.sequence, 0, cp["archives"]
            self.db.execute("PRAGMA incremental_vacuum")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return {"status": "checkpointed", "through": self.through, "compacted_events": compacted,
                    "archive_bytes": a["bytes"], "checkpoint_bytes": len(body), "seconds": time.monotonic() - began}
        except BaseException:
            self.poisoned = True
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def run_batch(self, *args, **kwargs):
        self.usable()
        require(self.context["evidence_domain"] == "checker-receipts", "simulation history cannot run a real checker")
        return super().run_batch(*args, **kwargs)

    def revise(self, *args, **kwargs):
        self.usable()
        return super().revise(*args, **kwargs)

    def status(self):
        self.usable()
        return {**super().status(), "checkpoint_through": self.through, "active_events": self.tail_count,
                "audit_archives": len(self.archives), "startup_events_replayed": self.replayed_events,
                "startup_archive_bytes_verified": self.verified_archive_bytes, "evidence_domain": self.context["evidence_domain"]}


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def import_v1_copy(source, destination):
    """Explicit v1 -> checkpoint storage copy. Source is locked and read-only.

    Candidate/checker semantics MUST be byte-identical; this is not a general
    software-upgrade escape hatch. Every source receipt is replayed and retained.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    require(source.is_dir() and not destination.exists() and destination.parent.is_dir(), "invalid import paths")
    require((source / "owner.lock").is_file(), "source lock missing")
    require(destination != source and source not in destination.parents, "destination must be outside source")
    with (source / "owner.lock").open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("source campaign already has an owner") from None
        stage = Path(tempfile.mkdtemp(prefix="checkpoint-import-", dir=destination.parent))
        snapshot = tempfile.TemporaryDirectory(prefix="v1-snapshot-", dir=stage)
        old = None
        c = None
        try:
            # Even SQLite mode=ro can write its shared-memory index. Copy the
            # database and committed WAL while holding the cooperative owner lock;
            # build/recover the WAL index only in our private snapshot. Do not copy
            # or open the source SHM file. No SQLite API touches the source store.
            for filename in ("history.sqlite", "history.sqlite-wal"):
                original = source / filename
                if not original.exists() and filename.endswith("-wal"):
                    continue
                require(original.is_file() and not original.is_symlink() and original.stat().st_size <= v1.CAP,
                        "invalid source database/WAL")
                disk_guard(stage, original.stat().st_size + 4 * 1024**2)
                copied = Path(snapshot.name) / filename
                with original.open("rb") as inp, copied.open("xb") as out:
                    shutil.copyfileobj(inp, out, 1024**2)
                require(sha(copied) == sha(original), "source changed during snapshot")
            old = sqlite3.connect(Path(snapshot.name) / "history.sqlite")
            require(old.execute("PRAGMA quick_check").fetchone() == ("ok",), "source database damaged")
            (stage / "plans").mkdir(mode=0o700)
            (stage / "targets").mkdir(mode=0o700)
            c = CheckpointCampaign(stage, _creating=True, checkpoint_every=0)
            c._legacy_replay = True
            # Retain complete immutable evidence, never load arbitrary rows as a
            # trusted aggregate checkpoint. Paths are exact digest filenames.
            for kind in ("plans", "targets"):
                for path in sorted((source / kind).iterdir()):
                    if path.name.startswith(".publish-"):
                        continue  # uncommitted publication, not authoritative
                    require(digest_string(path.name) and path.is_file() and not path.is_symlink(), "unexpected source artifact")
                    require(c.archive(path, kind) == path.name, "source artifact damaged")
            old.execute("BEGIN")
            c.db.execute("BEGIN IMMEDIATE")
            try:
                for row in old.execute("SELECT seq,previous,digest,body FROM events ORDER BY seq"):
                    require(c.sequence < v1.EVENT_CAP, "v1 journal exceeds admitted version")
                    c.apply_row(row)
                    c.db.execute("INSERT INTO tail VALUES(?,?,?,?)", row)
                    c.tail_count += 1
                require(c.state is not None, "empty source journal")
                c.db.execute("UPDATE tip SET seq=?,head=? WHERE id=1", (c.sequence, c.head))
                c.db.execute("COMMIT")
                old.execute("COMMIT")
            except BaseException:
                if c.db.in_transaction:
                    c.db.execute("ROLLBACK")
                raise
            origin = {"head": c.head, "events": c.sequence, "source": str(source)}
            # Upgrade is an audited event; frozen target/core/checker never change.
            new_context = dict(c.context, storage_schema=SCHEMA, storage_sha256=sha(__file__), evidence_domain="checker-receipts")
            c.append({"kind": "upgrade-v1-copy", "context": new_context,
                      "origin_head": c.head, "origin_events": c.sequence})
            c.checkpoint()
            c.audit()
            result = {"origin": origin, "destination": str(destination), "status": c.status()}
            c.close()
            c = None
            old.close()
            old = None
            snapshot.cleanup()
            sync_dir(stage)
            os.rename(stage, destination)
            sync_dir(destination.parent)
            return result
        finally:
            if old is not None:
                old.close()
            snapshot.cleanup()
            if c is not None:
                c.close()
            # On failure, retain the private staging directory for inspection.
            # Source evidence is never modified or removed.


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-synthetic")
    create.add_argument("directory", type=Path)
    create.add_argument("plan", type=Path)
    create.add_argument("target", type=Path)
    migrate = commands.add_parser("import-v1-copy")
    migrate.add_argument("source", type=Path)
    migrate.add_argument("destination", type=Path)
    for name in ("status", "run", "revise", "checkpoint", "audit"):
        s = commands.add_parser(name)
        s.add_argument("directory", type=Path)
        if name == "run":
            s.add_argument("--count", type=int, default=1_000_000)
        if name == "revise":
            s.add_argument("plan", type=Path)
    args = p.parse_args()
    if args.command == "import-v1-copy":
        result = import_v1_copy(args.source, args.destination)
    elif args.command == "create-synthetic":
        checker = str(BUILD / "synthetic-checker")
        with CheckpointCampaign.create(args.directory, args.plan, args.target, [checker, "check", "all", "ok"], [checker, "confirm"]) as c:
            result = c.status()
    else:
        with CheckpointCampaign(args.directory) as c:
            if args.command == "run":
                result = c.run_batch(args.count)
            elif args.command == "revise":
                result = c.revise(args.plan)
            else:
                result = getattr(c, args.command)()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
