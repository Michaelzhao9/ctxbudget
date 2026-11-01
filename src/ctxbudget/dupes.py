"""Near-duplicate detection between always-injected sources."""

from itertools import combinations

from .tokens import normalize_for_comparison

SHINGLE_SIZE = 6
MIN_TOKENS = 12
THRESHOLD = 0.5


def _shingles(text):
    words = normalize_for_comparison(text).split()
    if len(words) < MIN_TOKENS:
        return None
    return {
        " ".join(words[i:i + SHINGLE_SIZE])
        for i in range(len(words) - SHINGLE_SIZE + 1)
    }


def find_duplicates(sources, threshold=THRESHOLD, limit=10):
    """Return pairs of per-turn sources whose text overlaps heavily.

    ``redundant_tokens`` is the smaller side of the pair: the most a reader could
    save by deleting one of the two. Comparison is on 6-token shingles after
    case/punctuation folding; sources under ``MIN_TOKENS`` are skipped because
    short instruction lines overlap by accident.
    """
    candidates = []
    for source in sources:
        if not source.per_turn:
            continue
        shapes = _shingles(source.text or "")
        if shapes:
            candidates.append((source, shapes))

    pairs = []
    for (a, sa), (b, sb) in combinations(candidates, 2):
        union = len(sa | sb)
        if not union:
            continue
        similarity = len(sa & sb) / float(union)
        if similarity >= threshold:
            pairs.append(
                {
                    "a": a.label,
                    "b": b.label,
                    "similarity": round(similarity, 3),
                    "redundant_tokens": min(a.tokens, b.tokens),
                }
            )
    pairs.sort(key=lambda p: (-p["redundant_tokens"], p["a"], p["b"]))
    return pairs[:limit]
