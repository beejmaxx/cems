"""Equality-fixture identities are safety labels, never real-checker evidence."""
from layout import BUILD, digest

# Retained fixtures remain synthetic even when copied or their build is absent.
RETAINED_EQUALITY = {
    "c62d4de8bb95615b6f42726769e307dbcf8825d70806d01b51a2957585d1ef3d",
    "09f7d2cecc228f266ff7288ce8d427f86f985c515efe5a38bb670df008098e9f",
}


def synthetic_checker_identities():
    return RETAINED_EQUALITY | {
        digest(p) for p in (BUILD / "synthetic-checker", BUILD / "synthetic-checker-sanitized")
        if p.is_file()}
