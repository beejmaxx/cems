"""Read-only links from recollection evidence to recipe controls, plus review notes.

These records are not compiler inputs. Writing a note never changes weights.
"""
from datetime import datetime, timezone
import json
from pathlib import Path

from recipe import require
from runner import sha

SCHEMA = "recollect-memory-map-v1"
MAX_BYTES = 512 * 1024


def read_object(path):
    require(path.stat().st_size <= MAX_BYTES, "memory file exceeds 512 KiB")
    data = json.loads(path.read_text())
    require(isinstance(data, dict), "memory file must contain an object")
    return data


def load_map(source, source_sha):
    path = source.with_suffix(".memories.json")
    if not path.exists():
        return {"schema": SCHEMA, "cards": [], "layout": [], "message": "No recollection map has been supplied for this recipe. You can record notes below; they will remain pending translation into rules."}
    value = read_object(path)
    require(value.get("schema") == SCHEMA, "unsupported recollection map")
    require(value.get("source_sha256") == source_sha, "recollection map belongs to a different source recipe")
    require(isinstance(value.get("cards"), list) and len(value["cards"]) <= 100, "invalid recollection cards")
    ids = set()
    for card in value["cards"]:
        require(isinstance(card, dict) and isinstance(card.get("id"), str) and card["id"] not in ids, "invalid recollection id")
        ids.add(card["id"])
        for field in ("title", "group", "kind", "record", "model", "gap"):
            require(isinstance(card.get(field), str), "missing recollection field: " + field)
        require(isinstance(card.get("sources", []), list) and isinstance(card.get("controls", []), list), "invalid recollection links")
    require(isinstance(value.get("layout", []), list), "invalid memory layout")
    return value


def read_notes(path):
    if not path.exists():
        return {"schema": "recollect-memory-notes-v1", "entries": []}, None
    value = read_object(path)
    require(value.get("schema") == "recollect-memory-notes-v1" and isinstance(value.get("entries"), list), "invalid memory notes")
    return value, sha(path)


def append_note(path, mapping, card_id, text, expected, save):
    require(card_id == "unplaced" or card_id in {c["id"] for c in mapping["cards"]}, "unknown recollection topic")
    require(isinstance(text, str) and 1 <= len(text.strip()) <= 8000, "write a note of 1..8000 characters")
    value, revision = read_notes(path)
    require(revision == expected, "memory notes changed in another tab; reload before saving")
    require(len(value["entries"]) < 1000, "memory notebook is full")
    entry = {"topic": card_id, "text": text.strip(), "recorded_utc": datetime.now(timezone.utc).isoformat(),
             "speaker": "user", "status": "awaiting model review"}
    value["entries"].append(entry)
    require(len(json.dumps(value).encode()) <= MAX_BYTES, "memory notebook is full")
    save(path, value)
    return value, sha(path)
