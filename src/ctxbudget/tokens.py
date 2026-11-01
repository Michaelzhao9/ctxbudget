"""Token counting.

Two modes, both reported honestly in every output:

* ``tiktoken:<encoding>`` — exact BPE counts, used only when the optional
  ``tiktoken`` package is importable.
* ``heuristic:max(chars/4,words*4/3)+cjk`` — a documented estimator, used
  otherwise. It intentionally over-counts CJK (>= 1 token per character) because
  the cheap ``chars/4`` rule under-prices Chinese/Japanese/Korean text badly, and
  under-reporting cost is the failure mode this tool exists to fix.

The estimator is never presented as an exact number: ``tokenizer_info()`` is
embedded in every report.
"""

import re
import unicodedata

_CJK_RANGES = (
    (0x2E80, 0x9FFF),  # CJK radicals, kana, unified ideographs
    (0xA960, 0xA97F),  # Hangul jamo extended-A
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFF9F),  # half/full-width forms
    (0x20000, 0x2FA1F),  # CJK extension B-F + compat supplement
)

_ENCODING = None
_TIKTOKEN_ERROR = None

try:  # pragma: no cover - optional dependency, exercised by the non-estimated path
    import tiktoken as _tiktoken

    _ENCODING = _tiktoken.get_encoding("cl100k_base")
except Exception as exc:  # ImportError, or a tiktoken data download failure
    _TIKTOKEN_ERROR = str(exc)


def _is_cjk(ch):
    cp = ord(ch)
    for low, high in _CJK_RANGES:
        if low <= cp <= high:
            return True
    return False


def _heuristic(text):
    cjk = 0
    rest = []
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            rest.append(ch)
    rest_text = "".join(rest)
    words = len(re.findall(r"\S+", rest_text))
    by_chars = len(rest_text) / 4.0
    by_words = words * 4.0 / 3.0
    ascii_estimate = max(by_chars, by_words)
    return int(round(cjk * 1.0 + ascii_estimate))


def estimate_tokens(text):
    """Return the estimated token count for ``text`` (0 for empty input)."""
    if not text:
        return 0
    if _ENCODING is not None:  # pragma: no cover - depends on optional install
        return len(_ENCODING.encode(text, disallowed_special=()))
    return _heuristic(text)


def tokenizer_info():
    """Describe the counter actually in use so no report can imply exactness."""
    if _ENCODING is not None:  # pragma: no cover
        return {
            "name": "tiktoken:cl100k_base",
            "estimated": False,
            "note": "exact BPE counts for the cl100k_base vocabulary",
        }
    return {
        "name": "heuristic:max(chars/4,words*4/3)+cjk",
        "estimated": True,
        "note": (
            "estimator, not a tokenizer: non-CJK priced at max(chars/4, words*4/3), "
            "CJK at 1 token per character; real BPE counts may differ by ~15-25% "
            "(install tiktoken for exact counts)"
        ),
    }


def normalize_for_comparison(text):
    """Casefold, split CJK into characters, strip punctuation; used by dupe detection."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    spaced = "".join(
        " %s " % ch if _is_cjk(ch) else ch for ch in folded
    )
    return re.sub(r"[^\w\u2e80-\u9fff]+", " ", spaced).strip()
