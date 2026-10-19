"""PDF text via PyPDF2 when available.

PDF is the one format this build cannot handle with the standard library alone. This
extractor degrades loudly: an unavailable or failing parser is recorded as a
reason on the item, never as a silent empty result. A PDF with no text layer
is reported as needing OCR rather than as an empty document.
"""

from __future__ import annotations

import io

from . import ExtractedText, Limits, register

MAX_PAGES = 300


@register(["pdf"], ["application/pdf"], "pdf")
def extract_pdf(name: str, data: bytes, limits: Limits) -> ExtractedText:
    try:
        from PyPDF2 import PdfReader  # type: ignore
    except ImportError:
        return ExtractedText(reason="PyPDF2 not installed; PDF text unavailable")

    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return ExtractedText(reason="PDF is encrypted")
        pages: list[str] = []
        total = 0
        for index, page in enumerate(reader.pages):
            if index >= MAX_PAGES:
                break
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - one bad page must not kill the file
                text = ""
            pages.append(text)
            total += len(text)
            if total > limits.max_text_chars:
                break
    except Exception as exc:  # noqa: BLE001
        return ExtractedText(reason=f"pdf parse failed: {type(exc).__name__}: {exc}")

    body = "\n".join(pages)
    if not body.strip():
        return ExtractedText(reason="no text layer (likely a scanned PDF) - needs OCR")
    return ExtractedText(body=body, truncated=total > limits.max_text_chars)
