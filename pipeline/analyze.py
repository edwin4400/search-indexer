"""Stage 3 - text normalization, folding, and stop words.

Deliberate divergence from X1 Search. X1 strips stop words at *index* time
(its shipped `english.stop` list), which saves index space but makes phrase
queries lossy. This build keeps the index lossless so `"exact phrase"` is exact,
and
applies the stop-word list to query analysis and keyword extraction instead.
"""

from __future__ import annotations

import os
import re
import unicodedata

# Words carrying no search signal. Modest list; queries made only of these get
# a warning rather than a silent empty result.
DEFAULT_STOP_WORDS = frozenset(
    """
    a an the and or but if then than of at by for with about against between
    into through during before after above below to from up down in out on off
    over under again further once here there when where why how all any both
    each few more most other some such no nor not only own same so than too
    very can will just don should now it its is are was were be been being
    have has had having do does did doing this that these those i me my we our
    """.split()
)

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_NOT_TEXT = re.compile(r"[^\x09\x0a\x0d\x20-\x7e\u00a0-\uffff]")
_RUNS_OF_SPACE = re.compile(r"[ \t\f\v]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")
_SURROGATES = re.compile(r"[\ud800-\udfff]")


def scrub(text: str) -> str:
    """Make a string storable: no lone surrogates, nothing UTF-8 cannot encode.

    Python's utf-16 codec accepts unpaired surrogates and hands them back as-is,
    so decoding something that was not really utf-16 produces text that SQLite
    cannot encode - a hard failure at insert time rather than merely bad text.
    Scrubbing here means no extractor's output can break the index.
    """
    if not text:
        return text
    if _SURROGATES.search(text):
        text = _SURROGATES.sub("\ufffd", text)
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def load_stop_words(path: str | None = None) -> frozenset[str]:
    words = set(DEFAULT_STOP_WORDS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                word = line.strip().lower()
                if word and not word.startswith("#"):
                    words.add(word)
    return frozenset(words)


def fold(text: str) -> str:
    """Case-fold and strip diacritics: 'Résumé' and 'resume' should match."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def strip_markup(text: str) -> str:
    return _TAG.sub(" ", _SCRIPT_STYLE.sub(" ", text))


def normalize(text: str) -> str:
    """Make arbitrary extracted text safe to store and index."""
    text = text.replace("\x00", " ")
    text = _NOT_TEXT.sub(" ", text)
    text = _RUNS_OF_SPACE.sub(" ", text)
    return scrub(_MANY_NEWLINES.sub("\n\n", text).strip())


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[0-9a-z_]+", fold(text)) if len(t) > 1]


def content_terms(text: str, stop_words: frozenset[str], limit: int = 25) -> list[tuple[str, int]]:
    """Most frequent non-stop words - a poor man's tag cloud."""
    counts: dict[str, int] = {}
    for token in tokenize(text):
        if token in stop_words:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:limit]


def query_is_only_stop_words(query: str, stop_words: frozenset[str]) -> bool:
    tokens = tokenize(query)
    return bool(tokens) and all(t in stop_words for t in tokens)
