"""Independent, bounded CPU-test oracle for exact mixture top-k ordering.

This deliberately keeps a small explicit byte set. It is test evidence, never
production recovery history, and calls no selector, verifier, or coverage store.
Frozen branch ``index.at`` and ``index.probability`` are its only distribution
operations. Each oracle belongs to one immutable model/weight combination;
construct a new oracle after a model or weight revision.
"""

from fractions import Fraction
import math
import time


MAX_PREFIX = 100_000
MAX_CANDIDATES = 100_000
MAX_CANDIDATE_BYTES = 32 * 1024**2


class _OracleLimit(Exception):
    pass


class BoundedMixtureOracle:
    """Cache exact scores while geometrically extending frozen branch prefixes.

    ``max_prefix`` bounds coordinates read per branch, including a next-unseen
    lookahead coordinate. ``max_candidates`` bounds the explicit byte set and
    ``max_candidate_bytes`` bounds the sum of those byte strings' lengths.
    A deadline is absolute ``time.monotonic()`` time. Individual index calls and
    a bounded sort are synchronous; deadlines are checked before and after work.
    """

    def __init__(self, snapshots, weights=None, *, initial_prefix=64,
                 max_prefix=MAX_PREFIX, max_candidates=MAX_CANDIDATES,
                 max_candidate_bytes=MAX_CANDIDATE_BYTES):
        if (type(initial_prefix) is not int or initial_prefix < 1
                or type(max_prefix) is not int or not 1 <= max_prefix <= MAX_PREFIX
                or type(max_candidates) is not int or not 1 <= max_candidates <= MAX_CANDIDATES
                or type(max_candidate_bytes) is not int
                or not 1 <= max_candidate_bytes <= MAX_CANDIDATE_BYTES):
            raise ValueError("invalid bounded oracle limits")
        self.snapshots = dict(snapshots)
        if not 1 <= len(self.snapshots) <= 64:
            raise ValueError("oracle requires 1-64 frozen branches")
        raw_weights = dict.fromkeys(self.snapshots, 1) if weights is None else dict(weights)
        if raw_weights.keys() != self.snapshots.keys():
            raise ValueError("one exact weight required per frozen branch")
        parsed = {}
        for name, value in raw_weights.items():
            if (isinstance(value, bool) or not isinstance(value, (int, str, Fraction))
                    or isinstance(value, str) and len(value) > 1500):
                raise ValueError("oracle weights must be exact integers or rationals")
            weight = Fraction(value)
            if weight < 0 or max(weight.numerator.bit_length(), weight.denominator.bit_length()) > 4096:
                raise ValueError("invalid exact oracle weight")
            parsed[name] = weight
        total = sum(parsed.values(), Fraction(0))
        if not total:
            raise ValueError("at least one positive oracle weight required")
        self.weights = {name: weight / total for name, weight in parsed.items() if weight}
        self.names = tuple(sorted(self.weights))
        self.indices = {name: self.snapshots[name].index for name in self.names}
        self.counts = {name: self.snapshots[name].count for name in self.names}
        if any(type(count) is not int or count < 1 for count in self.counts.values()):
            raise ValueError("frozen branches must have positive integer counts")
        if any(index.total_mass != 1 for index in self.indices.values()):
            raise ValueError("oracle requires normalized branch distributions")
        self.initial_prefix = min(initial_prefix, max_prefix)
        self.max_prefix, self.max_candidates = max_prefix, max_candidates
        self.max_candidate_bytes = max_candidate_bytes
        self.prefixes = dict.fromkeys(self.names, 0)
        self.coordinates_read = dict.fromkeys(self.names, 0)
        self._heads, self._last = {}, {}
        self._scores, self._ordered = {}, []
        self._candidate_bytes = self._probability_queries = self._sorts = 0
        self._dirty = False
        self._growth_target = self.initial_prefix
        self.total_seconds = 0.0

    @staticmethod
    def _check_deadline(deadline):
        if deadline is not None and time.monotonic() >= deadline:
            raise _OracleLimit("deadline")

    def _head(self, name, deadline):
        if self.prefixes[name] == self.counts[name]:
            return None
        if name not in self._heads:
            self._check_deadline(deadline)
            if self.coordinates_read[name] >= self.max_prefix:
                raise _OracleLimit("prefix_limit")
            value, probability = self.indices[name].at(self.prefixes[name])
            self.coordinates_read[name] += 1
            if not isinstance(value, bytes) or not isinstance(probability, (int, Fraction)):
                raise ValueError("frozen index must return bytes and exact probabilities")
            probability = Fraction(probability)
            if not 0 < probability <= 1:
                raise ValueError("invalid frozen branch probability")
            previous = self._last.get(name)
            if previous is not None and (-probability, value) <= (-previous[1], previous[0]):
                raise ValueError("frozen branch ordering is not descending probability/bytes")
            self._heads[name] = (value, probability)
            self._check_deadline(deadline)
        return self._heads[name]

    def _bound(self, deadline):
        bound = Fraction(0)
        for name in self.names:
            head = self._head(name, deadline)
            if head is not None:
                bound += self.weights[name] * head[1]
        return bound

    def _add(self, value, deadline):
        if value in self._scores:
            return
        self._check_deadline(deadline)
        if len(self._scores) >= self.max_candidates:
            raise _OracleLimit("candidate_limit")
        if self._candidate_bytes + len(value) > self.max_candidate_bytes:
            raise _OracleLimit("candidate_bytes_limit")
        score = Fraction(0)
        for name in self.names:
            self._check_deadline(deadline)
            probability = self.indices[name].probability(value)
            self._probability_queries += 1
            if not isinstance(probability, (int, Fraction)) or not 0 <= probability <= 1:
                raise ValueError("frozen inverse probability must be exact and normalized")
            score += self.weights[name] * probability
        if not score:
            raise ValueError("prefix candidate missing from the positive mixture support")
        self._scores[value] = score
        self._candidate_bytes += len(value)
        self._dirty = True
        self._check_deadline(deadline)

    def _grow(self, deadline):
        for name in self.names:
            target = min(self._growth_target, self.counts[name], self.max_prefix)
            while self.prefixes[name] < target:
                self._check_deadline(deadline)
                value, probability = self._head(name, deadline)
                self._add(value, deadline)
                self._last[name] = (value, probability)
                self.prefixes[name] += 1
                del self._heads[name]
        self._growth_target = min(self.max_prefix, self._growth_target * 2)

    def _eligible(self, wanted, completed, deadline):
        self._check_deadline(deadline)
        if self._dirty:
            self._ordered = sorted(self._scores.items(), key=lambda row: (-row[1], row[0]))
            self._sorts += 1
            self._dirty = False
            self._check_deadline(deadline)
        rows = []
        for ordinal, row in enumerate(self._ordered):
            if ordinal % 256 == 0:
                self._check_deadline(deadline)
            if row[0] not in completed:
                rows.append(row)
                if len(rows) == wanted:
                    break
        return rows

    def certify(self, wanted, completed=(), *, deadline=None):
        """Return expected rows only when exact top-k ordering is certified.

        ``completed`` is a bounded test membership container and is not retained
        or mutated. Changing it, including removing entries, is supported. An
        unresolved result always has ``expected=[]`` and an explicit limit.
        The returned certificate has no candidate bytes and is JSON serializable.
        """
        if type(wanted) is not int or not 0 <= wanted <= MAX_CANDIDATES:
            raise ValueError("invalid bounded oracle requested count")
        if (deadline is not None and (isinstance(deadline, bool)
                or not isinstance(deadline, (int, float)) or not math.isfinite(deadline))):
            raise ValueError("deadline must be finite absolute monotonic time")
        began = time.monotonic()
        before_candidates = len(self._scores)
        before_queries = self._probability_queries
        before_coordinates = sum(self.coordinates_read.values())
        rows, bound, proof, limit = [], None, None, None
        try:
            self._check_deadline(deadline)
            if wanted == 0:
                proof = "empty-request"
            while proof is None:
                bound = self._bound(deadline)
                rows = self._eligible(wanted, completed, deadline)
                if all(self.prefixes[name] == self.counts[name] for name in self.names):
                    proof = "support-exhausted"
                elif len(rows) == wanted and rows[-1][1] > bound:
                    proof = "strict-tail-bound"
                else:
                    self._grow(deadline)
        except _OracleLimit as exc:
            limit = str(exc)
        seconds = time.monotonic() - began
        self.total_seconds += seconds
        certified = proof is not None
        certificate = {
            "method": "bounded-frozen-prefix-exact-mixture-v1",
            "certified": certified, "proof": proof, "limit": limit,
            "wanted": wanted, "returned": len(rows) if certified else 0,
            "unseen_bound": str(bound) if bound is not None else None,
            "kth_score": str(rows[-1][1]) if len(rows) == wanted and rows else None,
            "prefixes": dict(self.prefixes), "branch_counts": dict(self.counts),
            "coordinates_read": dict(self.coordinates_read),
            "normalized_weights": {name: str(self.weights[name]) for name in self.names},
            "cached_candidates": len(self._scores), "cached_candidate_bytes": self._candidate_bytes,
            "new_candidates": len(self._scores) - before_candidates,
            "probability_queries": self._probability_queries,
            "new_probability_queries": self._probability_queries - before_queries,
            "new_coordinates": sum(self.coordinates_read.values()) - before_coordinates,
            "sorts": self._sorts, "seconds": seconds, "total_seconds": self.total_seconds,
            "max_prefix": self.max_prefix, "max_candidates": self.max_candidates,
            "max_candidate_bytes": self.max_candidate_bytes,
        }
        return {"certified": certified, "status": "certified" if certified else "unresolved",
                "expected": rows if certified else [], "certificate": certificate}
