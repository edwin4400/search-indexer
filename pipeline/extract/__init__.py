"""Stage 2 - text extraction.

Every extractor has the same shape: bytes in, `ExtractedText` out, where
sub-items are how containers (email, archive) hand nested payloads back to the
pipeline. That is the mechanism that makes attachments independently
searchable and resolvable to their parent.

The registry is intentionally a plain list of (extensions, mimes, function) so
that a priority-ordered, sandboxed, out-of-process implementation can replace
this module without changing any caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..analyze import scrub


@dataclass
class Limits:
    """Guard rails from the sketch: untrusted input gets bounded resources."""

    max_doc_bytes: int = 50 * 1024 * 1024          # 50 MB per file
    max_text_chars: int = 2_000_000                # 2M chars of extracted text
    max_subitems: int = 64                         # attachments per container
    max_depth: int = 3                             # nested container recursion
    max_zip_member_bytes: int = 16 * 1024 * 1024   # 16 MB per archive member


@dataclass
class SubItem:
    """A nested payload: an email attachment, or an archive member."""

    name: str
    mime: str
    data: bytes
    kind: str = "attachment"   # attachment | archive-member
    origin: str = ""           # how the source reopens this member, if it can


@dataclass
class ExtractedText:
    body: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    subitems: list[SubItem] = field(default_factory=list)
    truncated: bool = False
    reason: str | None = None   # set when text is unavailable or partial


# (extensions, mime prefixes, function, label)
REGISTRY: list[tuple[set[str], set[str], object, str]] = []


def register(extensions: list[str], mimes: list[str], label: str):
    def decorator(function):
        REGISTRY.append(
            (
                {e.lower().lstrip(".") for e in extensions},
                {m.lower() for m in mimes},
                function,
                label,
            )
        )
        return function

    return decorator


def extension_of(name: str) -> str:
    leaf = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""


def extractor_for(name: str, mime: str = ""):
    ext = extension_of(name)
    mime = (mime or "").lower()
    for extensions, mimes, function, label in REGISTRY:
        if ext in extensions:
            return function, label
        if mime and any(mime.startswith(m) for m in mimes):
            return function, label
    return None, None


def extract_bytes(name: str, data: bytes, limits: Limits, mime: str = "") -> ExtractedText:
    """Extract one blob, bounded by *limits*. Never raises."""
    function, label = extractor_for(name, mime)
    if function is None:
        # No handler claims this name. Refusing outright is wrong: a shell
        # redirect writes `commands.out`, a log may be `.log1`, and plenty of
        # text files have no extension at all. Try to recognise text instead of
        # giving up, and say plainly when the bytes look binary.
        return sniff_text(name, data, limits)
    try:
        result = function(name, data, limits)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the crawl
        return ExtractedText(reason=f"{label}: {type(exc).__name__}: {exc}")

    scrub_extracted(result)
    if result.body and len(result.body) > limits.max_text_chars:
        result.body = result.body[: limits.max_text_chars]
        result.truncated = True
    return result


def scrub_extracted(result: ExtractedText) -> ExtractedText:
    """Remove anything the index cannot store, wherever it came from.

    Applied to every extractor's output so no single format handler can break a
    crawl by returning text SQLite rejects.
    """
    result.body = scrub(result.body or "")
    if result.fields:
        result.fields = {
            scrub(str(key)): scrub(str(value)) for key, value in result.fields.items()
        }
    for item in result.subitems or []:
        item.name = scrub(item.name or "") or "attachment"
        item.mime = scrub(item.mime or "")
    return result


# How much of a file to judge, and how much non-printable content is tolerated.
SNIFF_SAMPLE_BYTES = 8192
SNIFF_NON_TEXT_RATIO = 0.30

# Extensions that are never text, whatever the bytes happen to look like. A byte
# heuristic alone misfires on a small binary whose header avoids NULs; a declared
# image or archive is not text by definition.
BINARY_EXTENSIONS = frozenset("""
    png jpg jpeg gif bmp tif tiff webp ico emf wmf heic heif
    mp3 wav flac m4a ogg wma mp4 mov avi mkv wmv webm
    exe dll sys msi ocx so dylib com scr cab obj lib pdb pyc class
    zip 7z rar gz bz2 xz iso img dmg jar apk ipa vhd wim
    ttf otf woff woff2 eot
    doc xls ppt msg ost pst db sqlite edb lnk
""".split())


def looks_like_text(data: bytes) -> bool:
    """Cheap binary/text decision: a NUL byte means binary, and so does a high
    proportion of control characters. Executables, images, archives and Office
    containers all trip the first test within the first few kilobytes."""
    sample = data[:SNIFF_SAMPLE_BYTES]
    if not sample:
        return False
    if b"\x00" in sample:
        return False
    whitespace = sample.count(b"\n") + sample.count(b"\r") + sample.count(b"\t")
    printable = sum(1 for byte in sample if 32 <= byte < 127 or byte >= 160)
    return (printable + whitespace) / len(sample) >= (1 - SNIFF_NON_TEXT_RATIO)


def sniff_text(name: str, data: bytes, limits: Limits) -> ExtractedText:
    """Fallback for names no extractor claims."""
    from ..analyze import normalize
    from .text import decode_bytes

    suffix = extension_of(name)
    if suffix in BINARY_EXTENSIONS:
        return ExtractedText(reason=f"'{suffix}' is a binary format")
    if not looks_like_text(data):
        return ExtractedText(
            reason=f"no extractor for '{suffix or '(no extension)'}' and the content is not text"
        )
    body = normalize(decode_bytes(data))
    if not body.strip():
        return ExtractedText(reason="recognised as text but contained nothing to index")
    return ExtractedText(body=body)


# Importing registers the handlers. Kept at the bottom so the submodules can
# import the names defined above without a cycle.
from . import archive, eml, ooxml, pdf, text  # noqa: E402,F401
