"""Local visual editor for unadopted recipes; never runs password checks."""
import argparse
import copy
from contextlib import redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from urllib.parse import urlsplit
import webbrowser

from recipe import MAX_RECIPE, compile_recipe, read_recipe, require
from recipe_preview import build_proposal, print_preview, terminate
from plan_inspect import rank_number, quoted
from runner import sha
from workspace import Workspace
from recipe_memories import append_note, load_map, read_notes
from recipe_views import Views

ASSET = Path(__file__).with_name("recipe_desk.html")


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is int:
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(toml_value(k) + " = " + toml_value(v) for k, v in value.items()) + " }"
    raise ValueError("recipe values must be strings, integers, lists or tables")


def recipe_bytes(recipe):
    # Inline tables preserve every supported recipe field without another dependency.
    raw = ("# Visual tuning proposal. Original recipe is unchanged.\n" +
           "\n".join(toml_value(k) + " = " + toml_value(v) for k, v in recipe.items()) + "\n").encode()
    require(len(raw) <= MAX_RECIPE, "recipe exceeds 256 KiB")
    require(tomllib.loads(raw.decode()) == recipe, "recipe cannot be represented as TOML")
    return raw


def effective_document(document, original):
    require(isinstance(document, dict) and set(document) == {"recipe", "disabled_templates", "disabled_options", "ranks", "count"}, "invalid editor document")
    recipe = copy.deepcopy(document["recipe"])
    require(isinstance(recipe, dict), "invalid recipe")
    for key in ("source", "history", "schema", "status"):
        require(recipe.get(key) == original.get(key), key + " is pinned and cannot be edited here")
    disabled = document["disabled_templates"]
    require(isinstance(disabled, list) and all(isinstance(v, str) for v in disabled), "invalid template selection")
    templates = recipe.get("templates", [])
    require(set(disabled) <= {t["id"] for t in templates}, "unknown disabled template")
    recipe["templates"] = [t for t in templates if t["id"] not in disabled]
    used = {t.get("family", "default") for t in recipe["templates"]}
    recipe["families"] = {k: v for k, v in recipe.get("families", {"default": 1}).items() if k in used}
    excluded = document["disabled_options"]
    require(isinstance(excluded, dict) and set(excluded) <= set(recipe["slots"]), "invalid slot selection")
    for name, indices in excluded.items():
        rows = recipe["slots"][name].get("options", [])
        require(isinstance(indices, list) and all(type(i) is int and 0 <= i < len(rows) for i in indices), "invalid disabled option")
        recipe["slots"][name]["options"] = [r for i, r in enumerate(rows) if i not in indices]
    ranks = document["ranks"]
    require(isinstance(ranks, list) and 1 <= len(ranks) <= 20 and all(isinstance(v, str) for v in ranks), "provide 1..20 rank marks")
    ranks = list(dict.fromkeys(rank_number(v) for v in ranks))
    count = document["count"]
    require(type(count) is int and 1 <= count <= 10, "sample count must be 1..10")
    return recipe, recipe_bytes(recipe), ranks, count


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".desk-save-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def public_report(report):
    result = copy.deepcopy(report)
    result["candidates"] = str(result["candidates"])
    for window in result["windows"]:
        window["rank"] = str(window["rank"])
        for row in window.get("rows", []):
            row["rank"] = str(row["rank"])
            row["text"] = quoted(bytes.fromhex(row["hex"]))
    for row in result.get("top", []):
        row["rank"] = str(row["rank"])
        row["text"] = quoted(bytes.fromhex(row["hex"]))
    return result


def worker(job_path):
    job = json.loads(Path(job_path).read_text())
    recipe, raw, ranks, count = effective_document(job["document"], job["original"])
    compile_recipe(recipe)
    destination = Path(job["destination"])
    build_proposal(recipe, raw, destination, reuse=job["reuse"])
    with redirect_stdout(io.StringIO()):
        report = print_preview(destination, ranks, count)
        report["top"] = print_preview(destination, [1], 10)["windows"][0].get("rows", [])
    atomic_json(destination / "desk-preview.json", public_report(report))


class Desk:
    def __init__(self, source, draft, output):
        self.source, self.draft, self.output = source.resolve(), draft.resolve(), output.resolve()
        require(self.source != self.draft, "draft must be separate from the source recipe")
        self.original = read_recipe(self.source)
        self.source_sha = sha(self.source)
        self.saved_sha = None
        self.notes_path = self.source.with_suffix(".memory-notes.json")
        self.document = {"recipe": copy.deepcopy(self.original), "disabled_templates": [], "disabled_options": {},
                         "ranks": ["1", "1b", "100b", "1t"], "count": 5}
        if self.draft.exists():
            require(self.draft.stat().st_size <= 1024*1024, "saved desk draft is too large")
            saved = json.loads(self.draft.read_text())
            require(saved.get("source_sha256") == self.source_sha, "original recipe changed; use --draft with a new filename to start a separate review")
            self.document = saved["document"]
            self.saved_sha = sha(self.draft)
        effective_document(self.document, self.original)
        self.lock = threading.Lock()
        self.view_lock = threading.Lock()
        self.views = None
        self.version = 1
        self.status = "queued"
        self.error = None
        self.progress = "Preparing the first preview…"
        self.report = self.previous = self.latest = None
        self.preview_version = None
        self.stop = threading.Event()
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def snapshot(self, include_document=False):
        with self.lock:
            result = {"version": self.version, "preview_version": self.preview_version, "status": self.status,
                      "error": self.error, "progress": self.progress, "report": self.report, "previous": self.previous,
                      "draft": str(self.draft), "source": str(self.source), "bundle": self.latest}
            result["visual_views"] = 1
            if include_document:
                result["document"] = copy.deepcopy(self.document)
            return result

    def inspect_view(self, body):
        require(isinstance(body, dict) and body.get("kind") in {"autopsy", "heatmap"}, "unknown inspection view")
        with self.lock:
            require(body.get("version") == self.version == self.preview_version and self.status == "ready",
                    "Wait for this draft's preview before inspecting it.")
            directory, version = self.latest, self.version
        require(self.view_lock.acquire(blocking=False), "Another view is being calculated. Try again when it finishes.")
        try:
            if self.views is None or str(self.views.directory) != directory:
                self.views = Views(directory)
            if body["kind"] == "autopsy":
                result = self.views.autopsy(rank_number(str(body.get("rank", "1"))))
            else:
                require(isinstance(body.get("template"), str), "select a template")
                result = self.views.heatmap(body["template"], rank_number(str(body.get("budget", "1b"))),
                                           body.get("row_start", 0), body.get("col_start", 0))
            with self.lock:
                require(version == self.version, "Draft changed during inspection; calculate the new preview.")
            return dict(result, version=version)
        finally:
            self.view_lock.release()

    def memories(self):
        with self.lock:
            mapping = load_map(self.source, self.source_sha)
            notes, revision = read_notes(self.notes_path)
            return {"mapping": mapping, "notes": notes["entries"], "revision": revision}

    def record_memory(self, card_id, text, revision):
        with self.lock:
            mapping = load_map(self.source, self.source_sha)
            notes, revision = append_note(self.notes_path, mapping, card_id, text, revision, atomic_json)
            return {"mapping": mapping, "notes": notes["entries"], "revision": revision}

    def submit(self, document, version):
        effective_document(document, self.original)
        with self.lock:
            require(version == self.version, "another tab changed this draft; reload before editing")
            require(sha(self.source) == self.source_sha, "original recipe changed on disk; restart with a new draft")
            current = sha(self.draft) if self.draft.exists() else None
            require(current == self.saved_sha, "draft changed outside this desk; restart to load it")
            atomic_json(self.draft, {"schema": "recollect-desk-v1", "source_sha256": self.source_sha, "document": document})
            self.saved_sha = sha(self.draft)
            self.document = copy.deepcopy(document)
            self.version += 1
            self.status, self.error, self.progress = "queued", None, "Draft saved. Waiting for your latest change…"
            return self.version

    def run(self):
        attempted = 0
        while not self.stop.wait(0.1):
            with self.lock:
                version = self.version
                if version == attempted:
                    continue
                document = copy.deepcopy(self.document)
                reuse = self.latest
            if self.stop.wait(0.5):
                break
            with self.lock:
                if version != self.version:
                    continue
                attempted = version
                self.status, self.progress = "building", "Compiling and ranking your proposal…"
            try:
                with tempfile.TemporaryDirectory(prefix=".desk-building-", dir=self.output) as temp:
                    temp = Path(temp)
                    job = {"document": document, "original": self.original, "reuse": reuse, "destination": str(temp / "bundle")}
                    atomic_json(temp / "job.json", job)
                    log_path = temp / "build.log"
                    with log_path.open("w") as log:
                        proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--job", str(temp / "job.json")],
                                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                        try:
                            obsolete = False
                            while proc.poll() is None:
                                self.stop.wait(0.15)
                                with self.lock:
                                    obsolete = self.stop.is_set() or self.version != version
                                    lines = log_path.read_text(errors="replace").splitlines()
                                    if not obsolete and lines:
                                        self.progress = lines[-1][:500]
                                if obsolete:
                                    break
                            if obsolete or self.stop.is_set():
                                continue
                            require(proc.returncode == 0, "\n".join(log_path.read_text(errors="replace").splitlines()[-8:]) or "Preparation failed")
                        finally:
                            terminate(proc)
                    report = json.loads((temp / "bundle" / "desk-preview.json").read_text())
                    with self.lock:
                        if self.version != version or self.stop.is_set():
                            continue
                        destination = self.output / ("desk-" + time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3))
                        (temp / "bundle").rename(destination)
                        self.previous, self.report = self.report, report
                        self.latest, self.preview_version = str(destination), version
                        self.status, self.error = "ready", None
                        self.progress = "Preview ready. This remains an unadopted proposal."
            except Exception as error:
                with self.lock:
                    if self.version == version:
                        self.status, self.error = "error", str(error)
                        self.progress = "Could not build this draft. Last successful preview is retained."

    def close(self):
        self.stop.set()
        self.thread.join(timeout=10)


def handler_for(desk, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, data, content_type="application/json"):
            raw = json.dumps(data).encode() if content_type == "application/json" else data
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed(self, api=False):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host or self.headers.get("Origin", "http://" + host) != "http://" + host:
                self.send(403, {"error": "local origin required"}); return False
            if api and not secrets.compare_digest(self.headers.get("X-Desk-Token", ""), token):
                self.send(403, {"error": "open the URL printed by recollect desk"}); return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.allowed(api=path.startswith("/api/")):
                return
            if path == "/":
                self.send(200, ASSET.read_bytes(), "text/html")
            elif path == "/views.js":
                self.send(200, ASSET.with_name("recipe_views.js").read_bytes(), "text/javascript")
            elif path == "/api/memories":
                try:
                    self.send(200, desk.memories())
                except (ValueError, KeyError, TypeError, OSError) as error:
                    self.send(400, {"error": str(error)})
            elif path in {"/api/state", "/api/status"}:
                self.send(200, desk.snapshot(include_document=path == "/api/state"))
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.allowed(api=True):
                return
            if self.path not in {"/api/draft", "/api/memories", "/api/view"}:
                self.send(404, {"error": "not found"}); return
            try:
                require(self.headers.get("Content-Type") == "application/json", "JSON required")
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= 1024*1024, "request too large or empty")
                body = json.loads(self.rfile.read(size))
                if self.path == "/api/view":
                    self.send(200, desk.inspect_view(body))
                    return
                if self.path == "/api/memories":
                    self.send(200, desk.record_memory(body["topic"], body["text"], body["revision"]))
                    return
                version = desk.submit(body["document"], body["version"])
                self.send(200, {"version": version})
            except (ValueError, KeyError, TypeError, OSError, RuntimeError, argparse.ArgumentTypeError) as error:
                self.send(400, {"error": str(error)})
    return Handler


def main():
    if sys.argv[1:2] == ["--job"]:
        worker(sys.argv[2]); return
    parser = argparse.ArgumentParser(prog="recollect desk", description=__doc__)
    parser.add_argument("recipe", nargs="?", type=Path, help="defaults to workspace models/RECIPE.toml")
    parser.add_argument("--draft", type=Path, help="persistent editor draft; default RECIPE.desk.json next to recipe")
    parser.add_argument("--output", type=Path, help="proposal bundles; default workspace prepared-models")
    parser.add_argument("--port", type=int, default=0, help="loopback port; default chooses a free port")
    parser.add_argument("--no-open", action="store_true", help="print URL without opening browser")
    args = parser.parse_args()
    source = (args.recipe or Workspace().resolve("models/RECIPE.toml")).expanduser().resolve()
    draft = (args.draft or source.with_suffix(".desk.json")).expanduser()
    output = (args.output or Workspace().resolve("prepared-models")).expanduser()
    token = secrets.token_urlsafe(32)
    # Bind before starting preparation, so a busy port cannot leave an orphan worker.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), BaseHTTPRequestHandler)
    desk = None
    try:
        desk = Desk(source, draft, output)
        server.RequestHandlerClass = handler_for(desk, token)
        url = f"http://127.0.0.1:{server.server_port}/#{token}"
        print("Recollect tuning desk — local proposals only\n" + url + "\nCtrl-C stops the desk. Draft changes are saved automatically.", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        if desk:
            desk.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        raise SystemExit("recollect desk: " + str(error)) from None
