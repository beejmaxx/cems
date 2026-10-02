"""Single-owner, batch-only controller for the native preparation experiment.

Candidates flow C++ -> pipe -> checker, NEVER through this Python controller.
This is a local trusted-checker prototype, not a distributed or untrusted service.
"""
from layout import BUILD, runtime_source_matches
import argparse
import fcntl
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

from layout import LAB as HERE
CAP = 256 * 1024**2
RESERVE = 10 * 1024**3
PLAN_CAP = 64 * 1024**2
LOG_CAP = 64 * 1024
EVENT_CAP = 100_000
MAX_COORD = 2**63 - 1
ZERO = "0" * 64


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            h.update(block)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def natural(n, maximum=MAX_COORD):
    return type(n) is int and 0 <= n <= maximum


def disk_guard(root, reservation=0):
    used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    require(used + reservation <= CAP, "campaign storage cap")
    require(shutil.disk_usage(root).free >= RESERVE + reservation, "free disk reserve")
    return used


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def child_limits(cap=LOG_CAP):
    # Bounds diagnostics/acknowledgments, NOT the candidate pipe. No threads here.
    resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))


def read_small(stream):
    stream.seek(0)
    data = stream.read(LOG_CAP + 1)
    require(len(data) <= LOG_CAP, "oversized process result")
    return data


def execute(argv, *, timeout=100, data=None, file_cap=LOG_CAP):
    """Bounded process result. Used for control operations, never a candidate batch."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.run(list(map(str, argv)), input=data, stdout=out, stderr=err,
                           timeout=timeout, preexec_fn=partial(child_limits, file_cap))
        result, error = read_small(out), read_small(err)
        require(p.returncode == 0, "process failed: " + error.decode(errors="replace")[:1000])
        return result


def pin(argv):
    require(isinstance(argv, list) and argv and all(type(x) is str for x in argv), "invalid adapter argv")
    path = Path(argv[0]).resolve(strict=True)
    require(path.is_file() and os.access(path, os.X_OK), "adapter must be executable")
    return {"argv": [str(path), *argv[1:]], "sha256": sha(path)}


def checked_ack(ack, job, context):
    fields = {"schema", "job", "plan", "target", "submitted", "negative_prefix", "status"}
    require(type(ack) is dict and ack.get("status") in {"negative", "partial", "hit"}, "invalid acknowledgment")
    require(set(ack) == fields | ({"hit_hex"} if ack["status"] == "hit" else set()), "acknowledgment fields")
    require(ack["schema"] == "checked-prefix-v1" and ack["job"] == job["id"] and
            ack["plan"] == job["plan"] and ack["target"] == context["target"], "acknowledgment identity")
    require(type(ack["submitted"]) is int and ack["submitted"] == job["count"], "acknowledgment count")
    k = ack["negative_prefix"]
    require(natural(k, job["count"]), "invalid checked prefix")
    require((ack["status"] == "negative") == (k == job["count"]), "inconsistent acknowledgment status")
    if ack["status"] == "hit":
        h = ack["hit_hex"]
        require(type(h) is str and len(h) <= 256 and len(h) % 2 == 0 and
                all(c in "0123456789abcdef" for c in h), "invalid hit encoding")
    return k


class Campaign:
    """Exclusive local owner; event log is authority, derived cursor is expendable."""
    def __init__(self, root, *, _creating=False):
        self.root = Path(root).resolve()
        require(self.root.is_dir(), "campaign missing; resume never creates history")
        self.lock = None
        self.db = None
        self.state = None
        self.context = None
        self.info_cache = {}
        self.head = ZERO
        self.sequence = 0
        try:
            self.lock = (self.root / "owner.lock").open("a+b")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("campaign already has an owner") from None
            database = self.root / "history.sqlite"
            require(_creating or database.is_file(), "history database missing")
            self.db = sqlite3.connect(database, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA fullfsync=ON")
            self.db.execute("PRAGMA checkpoint_fullfsync=ON")
            self.db.execute("PRAGMA trusted_schema=OFF")
            if _creating:
                self.db.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY, previous TEXT NOT NULL, digest TEXT NOT NULL, body BLOB NOT NULL)")
                sync_dir(self.root)
            else:
                require(self.db.execute("PRAGMA quick_check").fetchone() == ("ok",), "database integrity failed")
                for seq, previous, digest, body in self.db.execute("SELECT seq,previous,digest,body FROM events ORDER BY seq"):
                    require(seq == self.sequence + 1 and seq <= EVENT_CAP and previous == self.head and
                            len(body) <= LOG_CAP and hashlib.sha256(previous.encode() + body).hexdigest() == digest,
                            "receipt chain damaged")
                    doc = json.loads(body)
                    require(canonical(doc) == body, "noncanonical receipt")
                    self.state = self.transition(doc)
                    self.sequence, self.head = seq, digest
                require(self.state is not None, "empty history is not a resumable campaign")
                self.verify_runtime()
        except BaseException:
            self.close()
            raise

    @classmethod
    def create(cls, root, plan, target, checker, confirmer, *, core=None):
        root = Path(root).resolve()
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        (root / "plans").mkdir(mode=0o700)
        (root / "targets").mkdir(mode=0o700)
        sync_dir(root.parent)
        c = cls(root, _creating=True)
        try:
            context = {"schema": "local-batch-runner-v1", "runner_sha256": sha(__file__),
                       "core": pin([str(core or BUILD / "prep")]), "checker": pin(checker),
                       "confirmer": pin(confirmer), "target": c.archive(target, "targets")}
            c.context = context
            p = c.archive(plan, "plans")
            c.append({"kind": "create", "context": context, "plan": p})
            c.verify_runtime()
            return c
        except BaseException:
            c.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def blob(self, digest, kind="plans"):
        require(type(digest) is str and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), "invalid blob identity")
        return self.root / kind / digest

    def archive(self, source, kind):
        source = Path(source)
        require(source.is_file() and source.stat().st_size <= PLAN_CAP, "artifact size limit")
        disk_guard(self.root, source.stat().st_size)
        fd, name = tempfile.mkstemp(prefix=".publish-", dir=self.root / kind)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
                written = 0
                for chunk in iter(lambda: inp.read(1024**2), b""):
                    written += len(chunk)
                    require(written <= PLAN_CAP, "artifact grew beyond bound")
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            digest = sha(temporary)
            destination = self.blob(digest, kind)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                require(sha(destination) == digest, "existing artifact corrupted")
            sync_dir(destination.parent)
            return digest
        finally:
            temporary.unlink(missing_ok=True)

    def verify_runtime(self):
        require(runtime_source_matches(self.context["runner_sha256"], __file__), "runner changed; explicit migration required")
        for name in ("core", "checker", "confirmer"):
            entry = self.context[name]
            require(sha(entry["argv"][0]) == entry["sha256"], name + " changed; explicit migration required")
        require(sha(self.blob(self.context["target"], "targets")) == self.context["target"], "target changed")

    def core(self, *args):
        # Preparing a plan writes a bounded regular file, unlike stream replay.
        cap = PLAN_CAP if args[0] == "subtract" else LOG_CAP
        return execute([*self.context["core"]["argv"], *args], file_cap=cap)

    def info(self, plan):
        if plan not in self.info_cache:
            path = self.blob(plan)
            require(path.is_file() and sha(path) == plan, "archived plan missing or damaged")
            info = json.loads(self.core("describe", path))
            require(0 <= int(info["candidates"]) <= MAX_COORD, "controller coordinate limit")
            self.info_cache[plan] = info
        return self.info_cache[plan]

    def transition(self, doc):
        require(type(doc) is dict, "invalid event")
        kind = doc.get("kind")
        if self.state is None:
            require(set(doc) == {"kind", "context", "plan"} and kind == "create", "missing creation event")
            self.context = doc["context"]
            require(self.context["schema"] == "local-batch-runner-v1", "unsupported campaign schema")
            self.verify_runtime()
            self.info(doc["plan"])
            return {"revision": 0, "plan": doc["plan"], "cursor": 0, "pending": None,
                    "hit": None, "ranges": {}, "checked_negative": 0, "receipts": 0}
        s = self.state.copy()
        if kind == "revise":
            require(set(doc) == {"kind", "full", "remaining", "history_head"} and
                    doc["history_head"] == self.head and s["hit"] is None, "invalid revision")
            self.info(doc["full"])
            self.info(doc["remaining"])
            s.update(revision=s["revision"] + 1, plan=doc["remaining"], cursor=0, pending=None)
        elif kind == "dispatch":
            require(set(doc) == {"kind", "job"} and s["pending"] is None and s["hit"] is None, "invalid dispatch")
            j = doc["job"]
            require(set(j) == {"id", "revision", "plan", "start", "count"} and
                    type(j["id"]) is str and len(j["id"]) == 32 and all(c in "0123456789abcdef" for c in j["id"]), "invalid job")
            require(j["revision"] == s["revision"] and j["plan"] == s["plan"] and j["start"] == s["cursor"] and
                    natural(j["start"]) and natural(j["count"], 100_000_000) and j["count"] > 0 and
                    j["start"] + j["count"] <= int(self.info(j["plan"])["candidates"]), "job outside active plan")
            s["pending"] = j
        elif kind == "complete":
            require(set(doc) == {"kind", "ack", "confirmation"} and s["pending"] is not None, "completion without pending job")
            j, ack = s["pending"], doc["ack"]
            k = checked_ack(ack, j, self.context)
            confirmation = doc["confirmation"]
            if ack["status"] == "hit":
                require(confirmation == {"schema": "confirmation-v1", "target": self.context["target"], "confirmed": True}
                        and type(confirmation["confirmed"]) is bool, "unconfirmed hit")
                s["hit"] = {"plan": j["plan"], "rank": j["start"] + k, "hex": ack["hit_hex"]}
            else:
                require(confirmation is None, "unexpected confirmation")
            if k:
                ranges = dict(s["ranges"])
                intervals = list(ranges.get(j["plan"], []))
                intervals.append((j["start"], j["start"] + k))
                merged = []
                for a, b in sorted(intervals):
                    if merged and a <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                    else:
                        merged.append((a, b))
                ranges[j["plan"]] = merged
                s["ranges"] = ranges
            s.update(cursor=j["start"] + k, pending=None, checked_negative=s["checked_negative"] + k,
                     receipts=s["receipts"] + 1)
        else:
            raise RuntimeError("unknown event")
        return s

    def append(self, doc):
        require(self.sequence < EVENT_CAP, "event log cap; explicit compaction/migration required")
        next_state = self.transition(doc)
        body = canonical(doc)
        require(len(body) <= LOG_CAP, "event size limit")
        digest = hashlib.sha256(self.head.encode() + body).hexdigest()
        disk_guard(self.root, 2 * 1024**2)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT INTO events VALUES(?,?,?,?)", (self.sequence + 1, self.head, digest, body))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        self.sequence += 1
        self.head, self.state = digest, next_state
        if self.sequence % 64 == 0:
            self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def revise(self, full_plan):
        self.verify_runtime()
        require(self.state["hit"] is None, "campaign has a confirmed hit")
        full = self.archive(full_plan, "plans")
        self.info(full)
        intervals = [(p, a, b-a) for p, ranges in sorted(self.state["ranges"].items()) for a, b in ranges]
        require(len(intervals) <= 512, "history interval limit; no exclusions were dropped")
        for p, _, _ in intervals:
            require(sha(self.blob(p)) == p, "history plan changed")
        disk_guard(self.root, 2 * PLAN_CAP)
        began = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="prepare-", dir=self.root) as tmp:
            prepared = Path(tmp) / "remaining.plan"
            metrics = json.loads(self.core("subtract", self.blob(full), prepared,
                                          *[x for p, a, n in intervals for x in (self.blob(p), a, n)]))
            remaining = self.archive(prepared, "plans")
            self.append({"kind": "revise", "full": full, "remaining": remaining, "history_head": self.head})
        return {"revision": self.state["revision"], "remaining": metrics, "history_intervals": len(intervals),
                "seconds": time.monotonic() - began}

    def status(self):
        s = self.state
        return {"revision": s["revision"], "plan": s["plan"], "cursor": s["cursor"],
                "available_in_plan": int(self.info(s["plan"])["candidates"]) - s["cursor"],
                "complete_remaining_support": self.info(s["plan"])["complete_remaining_support"],
                "pending": s["pending"], "hit": s["hit"], "checked_negative": s["checked_negative"],
                "receipts": s["receipts"], "events": self.sequence, "history_head": self.head,
                "history_intervals": sum(map(len, s["ranges"].values())), "storage_bytes": disk_guard(self.root)}

    def run_batch(self, count=1_000_000, *, timeout=100, fault=None):
        began = time.monotonic()
        require(natural(count, 100_000_000) and count > 0, "invalid batch limit")
        require(fault in {None, "before_commit", "after_commit"}, "invalid test fault")
        self.verify_runtime()
        s = self.state
        require(s["hit"] is None, "campaign has a confirmed hit")
        if s["pending"] is None:
            count = min(count, int(self.info(s["plan"])["candidates"]) - s["cursor"])
            if not count:
                return {"status": "plan_depleted", **self.status()}
            self.append({"kind": "dispatch", "job": {"id": uuid.uuid4().hex, "revision": s["revision"],
                        "plan": s["plan"], "start": s["cursor"], "count": count}})
        job = self.state["pending"]
        require(sha(self.blob(job["plan"])) == job["plan"], "active plan changed")
        disk_guard(self.root, 2 * 1024**2)
        producer = checker = None
        with tempfile.TemporaryFile() as producer_err, tempfile.TemporaryFile() as checker_out, tempfile.TemporaryFile() as checker_err:
            try:
                producer = subprocess.Popen([*self.context["core"]["argv"], "emit", str(self.blob(job["plan"])),
                                             str(job["start"]), str(job["count"])], stdout=subprocess.PIPE,
                                            stderr=producer_err, preexec_fn=child_limits)
                checker = subprocess.Popen([*self.context["checker"]["argv"], str(self.blob(self.context["target"], "targets")),
                                            job["id"], job["plan"], self.context["target"], str(job["count"])],
                                           stdin=producer.stdout, stdout=checker_out, stderr=checker_err, preexec_fn=child_limits)
                producer.stdout.close()
                checker.wait(timeout=timeout)
                producer.wait(timeout=max(0.1, timeout - (time.monotonic() - began)))
                require(producer.returncode == 0 and checker.returncode == 0,
                        "producer/checker failed; pending batch remains uncredited")
                metrics = json.loads(read_small(producer_err))
                require(type(metrics["frames"]) is int and metrics["frames"] == job["count"], "producer count mismatch")
                ack = json.loads(read_small(checker_out))
                k = checked_ack(ack, job, self.context)
            finally:
                for child in (checker, producer):
                    if child is not None and child.poll() is None:
                        child.kill()
                        child.wait()
                if producer and producer.stdout:
                    producer.stdout.close()
        confirmation = None
        if ack["status"] == "hit":
            line = self.core("inspect", self.blob(job["plan"]), job["start"] + k, 1).decode().rstrip("\n")
            require(line.split(" ")[0] == ack["hit_hex"], "hit is not the delivered candidate")
            value = bytes.fromhex(ack["hit_hex"])
            confirmation = json.loads(execute([*self.context["confirmer"]["argv"],
                str(self.blob(self.context["target"], "targets")), self.context["target"]],
                data=bytes([len(value)]) + value))
        if fault == "before_commit":
            raise RuntimeError("injected loss after acknowledgment, before commit")
        self.append({"kind": "complete", "ack": ack, "confirmation": confirmation})
        if fault == "after_commit":
            raise RuntimeError("injected loss after durable commit")
        seconds = time.monotonic() - began
        return {"status": ack["status"], "job": job, "generated_and_delivered": job["count"],
                "committed_negative": k, "confirmed_hits": int(ack["status"] == "hit"),
                "uncredited_tail": job["count"] - k - int(ack["status"] == "hit"),
                "seconds": seconds, "committed_negative_per_second": k / seconds, "producer": metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-synthetic", help="test equality predicate ONLY, no real recovery coverage")
    create.add_argument("directory", type=Path)
    create.add_argument("plan", type=Path)
    create.add_argument("target", type=Path, help="raw synthetic password bytes in a file")
    for verb in ("status", "run", "revise"):
        sub = commands.add_parser(verb)
        sub.add_argument("directory", type=Path)
        if verb == "run":
            sub.add_argument("--count", type=int, default=1_000_000)
        if verb == "revise":
            sub.add_argument("plan", type=Path)
    args = parser.parse_args()
    if args.command == "create-synthetic":
        binary = str(BUILD / "synthetic-checker")
        with Campaign.create(args.directory, args.plan, args.target, [binary, "check", "all", "ok"], [binary, "confirm"]) as c:
            print(json.dumps(c.status(), indent=2))
    else:
        with Campaign(args.directory) as c:
            result = c.run_batch(args.count) if args.command == "run" else c.revise(args.plan) if args.command == "revise" else c.status()
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
