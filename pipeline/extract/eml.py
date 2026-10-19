"""RFC 822 email (.eml) via the standard library.

Headers become searchable fields; the body becomes the indexed text; each
attachment becomes a sub-item and therefore its own indexed row with a parent
link. That is what makes "search inside an email attachment, and see which
email it came from" work.
"""

from __future__ import annotations

import email
import email.policy

from . import ExtractedText, Limits, SubItem, register
from .text import strip_html

HEADERS = ("From", "To", "Cc", "Bcc", "Subject", "Date", "Reply-To")


@register(["eml"], ["message/rfc822"], "email")
def extract_eml(name: str, data: bytes, limits: Limits) -> ExtractedText:
    message = email.message_from_bytes(data, policy=email.policy.default)

    fields: dict[str, str] = {}
    for header in HEADERS:
        value = message.get(header)
        if value:
            fields[header.lower()] = str(value).strip()

    body_parts: list[str] = []
    subitems: list[SubItem] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()

        # An attachment, whether or not it declares a filename.
        if filename or disposition == "attachment":
            if len(subitems) >= limits.max_subitems:
                continue
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001
                payload = b""
            if payload:
                subitems.append(
                    SubItem(name=filename or "attachment.bin", mime=content_type, data=payload)
                )
            continue

        try:
            if content_type == "text/plain":
                body_parts.append(part.get_content())
            elif content_type == "text/html":
                body_parts.append(strip_html(part.get_content()))
        except Exception:  # noqa: BLE001 - bad charset declarations are common
            raw = part.get_payload(decode=True) or b""
            body_parts.append(raw.decode("latin-1", errors="replace"))

    body = "\n".join(p for p in body_parts if p and p.strip())
    if not body.strip() and not subitems:
        return ExtractedText(fields=fields, reason="email had no readable body or attachments")
    return ExtractedText(body=body, fields=fields, subitems=subitems)
