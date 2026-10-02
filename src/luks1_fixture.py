"""Generate independent, disposable LUKS1 controls. NEVER rewrite a source header.

Only numeric geometry/profile fields are read from a supplied template. All
secrets, salts, UUID and ciphertext are newly generated. The control password is
deliberately public. PyCryptodome generates the fixture; QEMU verifies it.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import uuid

from runner import RESERVE, require, sha

ACTIVE, INACTIVE = 0xac71f3, 0xdead
PASSWORD = b"Coffee.8.Morning"


def profile(data):
    require(len(data) >= 592 and data[:8] == b"LUKS\xba\xbe\x00\x01", "not a LUKS1 header")
    text = lambda a: data[a:a+32].split(b"\0", 1)[0].decode("ascii")
    p = {"cipher": text(8), "mode": text(40), "hash": text(72),
         "payload_sector": struct.unpack_from(">I", data, 104)[0],
         "key_bytes": struct.unpack_from(">I", data, 108)[0],
         "digest_iterations": struct.unpack_from(">I", data, 164)[0], "slots": []}
    require((p["cipher"], p["mode"], p["hash"], p["key_bytes"]) ==
            ("aes", "cbc-essiv:sha256", "sha1", 32), "unsupported fixture profile")
    require(0 < p["digest_iterations"] <= 10000000, "digest iterations out of range")
    for i in range(8):
        a, n, _, o, s = struct.unpack_from(">II32sII", data, 208+48*i)
        require(a in (ACTIVE, INACTIVE) and 0 < s <= 524288 and o >= 2, "invalid fixture slot geometry")
        if a == ACTIVE:
            require(0 < n <= 10000000, "slot iterations out of range")
        p["slots"].append({"slot": i, "active": a == ACTIVE, "iterations": n, "offset_sector": o, "stripes": s})
    require(any(s["active"] for s in p["slots"]), "no enabled keyslot")
    require(0 < p["payload_sector"] <= 65536, "bounded attached control image required")
    return p


def default_profile():
    return {"cipher": "aes", "mode": "cbc-essiv:sha256", "hash": "sha1", "key_bytes": 32,
            "payload_sector": 2056, "digest_iterations": 34000,
            "slots": [{"slot": i, "active": i == 0, "iterations": 105474 if i == 0 else 106944 if i == 1 else 0,
                       "offset_sector": 8+256*i, "stripes": 4000} for i in range(8)]}


def diffuse(value):
    return b"".join(hashlib.sha1(struct.pack(">I", i//20)+value[i:i+20]).digest()[:len(value[i:i+20])]
                    for i in range(0, len(value), 20))


def crypt_sectors(key, data, *, decrypt=False):
    from Crypto.Cipher import AES
    require(len(data) % 512 == 0, "sector alignment")
    essiv = AES.new(hashlib.sha256(key).digest(), AES.MODE_ECB)
    out = bytearray()
    for i in range(0, len(data), 512):
        iv = essiv.encrypt(struct.pack("<Q", i//512)+bytes(8))
        cipher = AES.new(key, AES.MODE_CBC, iv)
        out.extend((cipher.decrypt if decrypt else cipher.encrypt)(data[i:i+512]))
    return bytes(out)


def image(p, password=PASSWORD, *, payload=None):
    """Test fixture only: construct a fresh valid image from public numeric fields."""
    payload = os.urandom(512) if payload is None else payload
    require(len(password) <= 128 and len(payload) == 512, "fixture input bounds")
    total = p["payload_sector"]*512+512
    require(total <= 34*1024**2, "fixture disk limit")
    data = bytearray(total)
    key, mk_salt = os.urandom(32), os.urandom(32)
    mk_digest = hashlib.pbkdf2_hmac("sha1", key, mk_salt, p["digest_iterations"], 20)
    header = struct.pack(">6sH32s32s32sII20s32sI40s", b"LUKS\xba\xbe", 1,
                         p["cipher"].encode(), p["mode"].encode(), p["hash"].encode(),
                         p["payload_sector"], 32, mk_digest, mk_salt, p["digest_iterations"], str(uuid.uuid4()).encode())
    data[:208] = header
    used = []
    for slot in p["slots"]:
        i, n, offset, stripes = (slot[k] for k in ("slot", "iterations", "offset_sector", "stripes"))
        salt = os.urandom(32) if slot["active"] or n else bytes(32)
        struct.pack_into(">II32sII", data, 208+48*i, ACTIVE if slot["active"] else INACTIVE, n, salt, offset, stripes)
        size = ((32*stripes+511)//512)*512
        a, b = offset*512, offset*512+size
        require(a >= 1024 and b <= p["payload_sector"]*512 and all(b <= x or y <= a for x,y in used),
                "overlapping/out-of-bounds fixture geometry")
        used.append((a,b))
        if not slot["active"]:
            if n: data[a:b] = b"\xff"*size # Erased former slot, as in the template.
            continue
        expanded = bytearray(os.urandom((stripes-1)*32))
        accumulator = bytes(32)
        for stripe in range(stripes-1):
            accumulator = diffuse(bytes(a^b for a,b in zip(accumulator, expanded[stripe*32:(stripe+1)*32])))
        expanded.extend(bytes(a^b for a,b in zip(accumulator, key)))
        expanded.extend(bytes(size-len(expanded)))
        slot_key = hashlib.pbkdf2_hmac("sha1", password, salt, n, 32)
        data[a:b] = crypt_sectors(slot_key, expanded)
    data[p["payload_sector"]*512:] = crypt_sectors(key, payload)
    return bytes(data), payload


def unwrap(data, password):
    """Independent Python digest oracle for tests, not the candidate hot path."""
    p = profile(data)
    for s in p["slots"]:
        if not s["active"]: continue
        salt = data[208+48*s["slot"]+8:208+48*s["slot"]+40]
        key = hashlib.pbkdf2_hmac("sha1", password, salt, s["iterations"], 32)
        n = ((32*s["stripes"]+511)//512)*512
        a = s["offset_sector"]*512
        require(a+n <= len(data), "missing key material")
        expanded = crypt_sectors(key, data[a:a+n], decrypt=True)
        accumulator = bytes(32)
        for stripe in range(s["stripes"]):
            accumulator = bytes(a^b for a,b in zip(accumulator, expanded[stripe*32:(stripe+1)*32]))
            if stripe+1 < s["stripes"]: accumulator = diffuse(accumulator)
        d = hashlib.pbkdf2_hmac("sha1", accumulator, data[132:164], p["digest_iterations"], 20)
        if hmac.compare_digest(d, data[112:132]): return accumulator
    return None


def write_new(path, data):
    with Path(path).open("xb") as f:
        os.chmod(path, 0o600)
        f.write(data)


def qemu_read(target, password, qemu):
    # Public synthetic passwords only. Private mode-0700 directory; no shell.
    with tempfile.TemporaryDirectory(prefix="luks1-control-oracle-") as temp:
        root = Path(temp); secret, out = root/"secret", root/"sector"
        write_new(secret, password)
        options = lambda p: str(Path(p).resolve()).replace(",", ",,")
        r = subprocess.run([str(qemu), "dd", "--object", f"secret,id=s,format=raw,file={options(secret)}",
             "--image-opts", "bs=512", "count=1",
             f"if=driver=luks,key-secret=s,file.driver=file,file.filename={options(target)}", f"of={out}"],
             capture_output=True, timeout=30)
        ok = r.returncode == 0 and out.is_file() and out.stat().st_size == 512
        return {"accepted": ok, "returncode": r.returncode,
                "plaintext_sha256": sha(out) if ok else None}


def build(directory, *, template=None, qemu):
    import shutil
    directory = Path(directory).resolve()
    require(not directory.exists(), "new control directory required")
    require(shutil.disk_usage(directory.parent).free >= RESERVE+64*1024**2, "disk reserve")
    source_sha = sha(template) if template else None
    if template: require(Path(template).is_file() and Path(template).stat().st_size <= 64*1024**2, "template file bound")
    p = profile(Path(template).read_bytes()) if template else default_profile()
    directory.mkdir(mode=0o700)
    entries = []
    original, _ = image(p, payload=bytes(512))
    master = unwrap(original, PASSWORD)
    require(master is not None, "generated fixture failed its own digest")
    for name, payload in (("low", bytes(512)), ("high", bytes(range(256))*2)):
        plaintext = payload
        data = original[:p["payload_sector"]*512]+crypt_sectors(master, payload)
        path = directory/f"SYNTHETIC-{name}.luks"
        write_new(path, data)
        require(profile(data) == p and unwrap(data, PASSWORD) is not None, "fixture profile/digest mismatch")
        positive = qemu_read(path, PASSWORD, qemu)
        require(positive["accepted"] and positive["plaintext_sha256"] == hashlib.sha256(plaintext).hexdigest(),
                "QEMU did not independently recover the exact control plaintext")
        negative = qemu_read(path, b"Coffee.9.Morning", qemu)
        require(not negative["accepted"] and unwrap(data, b"Coffee.9.Morning") is None, "wrong password accepted")
        entries.append({"file": path.name, "sha256": sha(path), "bytes": len(data),
                        "qemu_positive": positive, "qemu_negative": negative,
                        "plaintext_entropy_bits_per_byte": 0 if name == "low" else 8})
    if template: require(sha(template) == source_sha, "source header changed")
    doc = {"schema": "synthetic-target-matched-luks1-v1", "synthetic_only": True,
           "public_test_password_hex": PASSWORD.hex(), "template_sha256": source_sha,
           "profile": p, "controls": entries, "qemu": {"path": str(qemu), "sha256": sha(qemu)},
           "controls_share_identical_header_and_key_material": True,
           "differences": ["new volume key", "public control passphrase", "new salts", "new digest", "new UUID",
                           "new keyslot ciphertext", "new payload; none copied from the source volume"],
           "coverage": "Only generated targets; never coverage for the template/recovery target"}
    write_new(directory/"controls.json", (json.dumps(doc, indent=2)+"\n").encode())
    return doc


if __name__ == "__main__":
    import shutil
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--qemu", type=Path, default=shutil.which("qemu-img"))
    a = parser.parse_args()
    require(a.qemu is not None, "qemu-img is required")
    print(json.dumps(build(a.directory, template=a.template, qemu=a.qemu.resolve()), indent=2))
