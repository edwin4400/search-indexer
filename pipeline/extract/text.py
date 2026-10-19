"""Plain text, source code, markup, and delimited data."""

from __future__ import annotations

import re

from ..analyze import normalize, strip_markup
from . import ExtractedText, Limits, register

TEXT_EXTENSIONS = [
    "txt", "text", "log", "md", "markdown", "rst", "csv", "tsv", "json", "jsonl",
    "xml", "yml", "yaml", "toml", "ini", "cfg", "conf", "properties", "env",
    "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "cs", "java", "kt", "go", "rs",
    "c", "h", "cpp", "hpp", "m", "mm", "swift", "rb", "php", "pl", "sh", "ps1",
    "bat", "cmd", "sql", "r", "lua", "vue", "svelte", "css", "scss", "tex",
    # Command output and other plain-text files that carry no obvious suffix.
    # `.out` in particular is what a shell redirect produces.
    "out", "err", "stdout", "stderr", "lst", "nfo", "asc", "srt", "vtt", "diff",
    "patch", "reg", "service", "rules", "desktop", "po", "tcl", "fish", "zsh",
    "bash", "ksh", "rc", "config", "psm1", "vbs", "asm", "s", "m4", "mdown",
    "log1", "log2", "rtf_", "textfile",
]

HTML_EXTENSIONS = ["html", "htm", "xhtml", "mht"]


def decode_bytes(data: bytes, encoding: str | None = None) -> str:
    """Decode with a few sane guesses before giving up.

    utf-16 is only attempted when the bytes actually look like it. The codec
    accepts unpaired surrogates, so a mis-guess yields text SQLite cannot store.
    """
    candidates: list[str | None] = [encoding]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        candidates.append("utf-16")
    elif len(data) > 8 and (data[:1] == b"\x00" or data[1:2] == b"\x00"):
        # Alternating NULs are the signature of BOM-less UTF-16.
        candidates.append("utf-16-le" if data[1:2] == b"\x00" else "utf-16-be")
    candidates += ["utf-8-sig", "utf-8", "cp1252"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return data.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace")


def strip_html(text: str) -> str:
    return normalize(strip_markup(text))


@register(TEXT_EXTENSIONS, ["text/plain", "text/csv", "application/json"], "text")
def extract_text(name: str, data: bytes, limits: Limits) -> ExtractedText:
    body = decode_bytes(data)
    if name.rsplit(".", 1)[-1].lower() in HTML_EXTENSIONS:
        body = strip_html(body)
    else:
        body = normalize(body)
    if not body.strip():
        return ExtractedText(reason="file contained no text")
    return ExtractedText(body=body)
