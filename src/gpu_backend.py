"""Bounded Hashcat 7.1.2 / mode 29511 operations, with explicit legacy semantics.

This is NOT the digest-equivalent GPU verifier. Its negative results remain
qualified as Hashcat payload-entropy evidence. No progress-only acknowledgments.
"""
from layout import BUILD
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import time

from checkpoint_runner import parse
from luks1_fixture import profile
from runner import HERE, RESERVE, execute, natural, require, sha

POLICY = "hashcat-7.1.2-29511-payload-entropy-negatives-NOT-digest-equivalent-v1"
MARKER = "bounded-hashcat-file-batch-v1"
SPOOL = BUILD / "hex-spool"
MAX_BATCH = 1_000_000
LOG_MAX = 8 * 1024**2


def extract(data):
    """Exact stock format for the one supported active slot. No digest is added."""
    p = profile(data)
    slots = [s for s in p["slots"] if s["active"]]
    require(len(slots) == 1 and slots[0]["slot"] == 0 and slots[0]["stripes"] == 4000,
            "GPU handoff requires exactly slot 0, with 4000 stripes")
    s = slots[0]; start = s["offset_sector"]*512; end = start+128000
    off = p["payload_sector"]*512
    require(start >= 1024 and end <= off and len(data) >= off+512, "missing slot/payload bytes")
    fields = ["1", "sha1", "aes", "cbc-essiv:sha256", "256", str(s["iterations"]), data[216:248].hex(),
              data[start:end].hex(), data[off:off+512].hex()]
    return ("$luks$"+"$".join(fields)+"\n").encode()


def bind_target(header, target):
    header, target = Path(header), Path(target)
    require(header.is_file() and not header.is_symlink() and header.stat().st_size <= 64*1024**2,
            "regular bounded header required")
    require(target.is_file() and not target.is_symlink() and target.stat().st_size <= 1024**2,
            "regular bounded extract required")
    require(extract(header.read_bytes()) == target.read_bytes(), "header does not reproduce exact checking extract")
    return {"header_sha256": sha(header), "target_sha256": sha(target)}


def runtime_tree(root):
    root = Path(root)
    module = root/"modules/module_29511.so"
    require(module.is_file() and (root/"OpenCL/m14611-pure.cl").is_file(),
            "supply Hashcat installation root with OpenCL/ and modules/module_29511.so")
    files = sorted({module, *(root/"OpenCL").glob("*.cl"), *(root/"OpenCL").glob("*.h")})
    require(len(files) <= 5000 and sum(f.stat().st_size for f in files) <= 256*1024**2,
            "Hashcat runtime tree bound")
    table = {}
    for path in files:
        require(path.is_file() and not path.is_symlink(), "symlink in Hashcat runtime tree")
        table[str(path.relative_to(root))] = sha(path)
    # Compact root of the complete file inventory, not thousands of CLI args.
    return {"sha256": hashlib.sha256(json.dumps(table, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "files": len(table)}


def runtime_identity(executable, root):
    """Pin executable, kernel sources/includes and selected compiled module.

    Driver/runtime libraries are recorded by -I but not cryptographically pinned.
    This is a trusted-host contract, not attestation against a malicious helper.
    """
    executable, root = Path(executable).resolve(strict=True), Path(root).resolve(strict=True)
    require(executable.is_file() and os.access(executable, os.X_OK), "Hashcat executable required")
    tree = runtime_tree(root)
    version = execute([executable, "--version"], timeout=15).decode().strip()
    require(version == "v7.1.2", "this handoff admits Hashcat v7.1.2 only; other versions need explicit qualification")
    return {"executable": str(executable), "sha256": sha(executable), "root": str(root),
            "version": version, "runtime_tree": tree}


def verify_runtime(identity):
    require(sha(identity["executable"]) == identity["sha256"], "Hashcat executable changed")
    require(runtime_tree(identity["root"]) == identity["runtime_tree"], "Hashcat runtime changed")


def devices(value):
    require(value == "all" or re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", value), "invalid device selection")
    return [] if value == "all" else ["-d", value]


def finish_status(log, code, count, found):
    """Require final status, actual count, zero rejects, one hash and exit agreement."""
    statuses = []
    for line in log.decode(errors="replace").splitlines():
        if not line.lstrip().startswith("{"): continue
        try: item = parse(line)
        except (ValueError, RuntimeError): continue
        if type(item) is dict and "status" in item: statuses.append(item)
    require(statuses, "no terminal Hashcat JSON status; no coverage")
    last = statuses[-1]
    keys = ("status", "progress", "rejected", "recovered_hashes")
    require(all(k in last for k in keys), "incomplete Hashcat status")
    s = {k: last[k] for k in keys}
    require(type(s["status"]) is int and type(s["rejected"]) is int and s["rejected"] == 0,
            "Hashcat rejected inputs; no coverage")
    require(type(s["progress"]) is list and len(s["progress"]) == 2 and
            all(natural(n, count) for n in s["progress"]) and s["progress"][1] == count,
            "Hashcat input count differs from delivered count")
    require(type(s["recovered_hashes"]) is list and len(s["recovered_hashes"]) == 2 and
            all(type(n) is int for n in s["recovered_hashes"]), "bad recovered hash count")
    require(type(last.get("devices")) is list and last["devices"] and
            all(type(d) is dict and d.get("device_type") == "GPU" and
                type(d.get("device_id")) is int and type(d.get("device_name")) is str
                for d in last["devices"]), "no completed GPU device status")
    s["devices"] = [{k: d[k] for k in ("device_id", "device_name", "device_type")} for d in last["devices"]]
    if code == 1 and not found:
        require(s["status"] == 5 and s["progress"] == [count, count] and
                s["recovered_hashes"] == [0, 1], "not a complete zero-hit exhaustion")
    else:
        require(code == 0 and s["status"] == 6 and s["recovered_hashes"] == [1, 1] and len(found) == 1,
                "unfinished/error/ambiguous hit; no coverage")
    return dict(s, returncode=code, found_hex=[v.hex() for v in found],
                candidate_count=count, log_sha256=hashlib.sha256(log).hexdigest())


def kill_group(process):
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    process.wait()


def child_bound():
    # Hashcat may write compiled kernels. Bound each file; diagnostics get the
    # tighter polled bound below. Do not inherit the controller's 64KiB limit.
    resource.setrlimit(resource.RLIMIT_FSIZE, (256*1024**2, 256*1024**2))


def hashcat_run(identity, target, wordlist, count, directory, *, selected="all", timeout=600, limit=None):
    import shutil
    require(0 < count <= MAX_BATCH and 1 <= timeout <= 3600, "bounded Hashcat invocation required")
    require(limit is None or limit == count, "limit/count mismatch")
    verify_runtime(identity)
    directory = Path(directory); directory.mkdir(mode=0o700, exist_ok=False)
    target, wordlist = Path(target).resolve(), Path(wordlist).resolve()
    before = (sha(target), sha(wordlist))
    outfile, logfile = directory/"found.hex", directory/"hashcat.log"
    args = [identity["executable"], "-m", "29511", "-a", "0", "-D", "2", *devices(selected),
            "--hex-wordlist", "--potfile-disable", "--restore-disable", "--logfile-disable",
            "--runtime", str(int(timeout)), "--status", "--status-json", "--status-timer", "30", "--outfile", str(outfile.resolve()),
            "--outfile-format", "3", "--session", "bounded-"+directory.name]
    if limit is not None: args += ["--limit", str(limit)]
    args += [str(target), str(wordlist)]
    began = time.monotonic()
    with logfile.open("xb") as log:
        os.chmod(logfile, 0o600)
        process = subprocess.Popen(args, cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, preexec_fn=child_bound)
        try:
            while process.poll() is None:
                require(time.monotonic()-began < timeout, "Hashcat timeout; entire batch uncredited")
                require(logfile.stat().st_size <= LOG_MAX and
                        (not outfile.exists() or outfile.stat().st_size <= 512), "Hashcat output exceeded bound")
                require(shutil.disk_usage(directory).free >= RESERVE, "disk reserve exhausted")
                time.sleep(0.1)
        finally:
            if process.poll() is None: kill_group(process)
    require(logfile.stat().st_size <= LOG_MAX, "Hashcat diagnostics too large")
    found = []
    if outfile.exists():
        require(outfile.stat().st_size <= 512, "Hashcat hit output too large")
        for line in outfile.read_bytes().splitlines():
            require(re.fullmatch(rb"[0-9a-fA-F]{2,128}", line) and len(line) % 2 == 0, "invalid hex hit")
            found.append(bytes.fromhex(line.decode()))
    require(before == (sha(target), sha(wordlist)), "checking input changed during run")
    verify_runtime(identity)
    result = finish_status(logfile.read_bytes(), process.returncode, count, found)
    result["seconds"] = time.monotonic()-began
    return result


def spool(core, binary, plan, start, count, output):
    """Both per-candidate stages are native; no Python iterator over guesses."""
    require(natural(start) and 0 < count <= MAX_BATCH, "spool coordinate bounds")
    output = Path(output)
    producer = consumer = None
    import tempfile
    with tempfile.TemporaryFile() as pe, tempfile.TemporaryFile() as ce, output.open("xb") as out:
        os.chmod(output, 0o600)
        try:
            producer = subprocess.Popen([str(core), "emit", str(plan), str(start), str(count)],
                                        stdout=subprocess.PIPE, stderr=pe)
            consumer = subprocess.Popen([str(binary), str(count)], stdin=producer.stdout, stdout=out, stderr=ce)
            producer.stdout.close()
            consumer.wait(timeout=90); producer.wait(timeout=10)
            require(producer.returncode == consumer.returncode == 0, "native generation/spool failed; no coverage")
            pe.seek(0); ce.seek(0)
            a, b = parse(pe.read(65537)), parse(ce.read(65537))
            require(a["frames"] == b["frames"] == count, "native spool count disagreement")
            out.flush(); os.fsync(out.fileno())
            require(output.stat().st_size == b["bytes"] <= count*129, "native spool size disagreement")
            return {"frames": count, "bytes": b["bytes"], "sha256": sha(output)}
        finally:
            for process in (consumer, producer):
                if process and process.poll() is None: process.kill(); process.wait()


def hit_index(wordlist, value, count):
    # Runs only upon a hit; normal candidate generation never enters Python.
    needle = value.hex().encode()+b"\n"
    with Path(wordlist).open("rb") as inp:
        for i, line in enumerate(inp):
            require(i < count and len(line) <= 129, "wordlist bounds changed")
            if line == needle: return i
    raise RuntimeError("Hashcat reported a candidate that was not delivered")
