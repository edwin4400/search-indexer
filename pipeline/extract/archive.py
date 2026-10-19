"""ZIP archives: members become sub-items.

Member *names* are folded into the body text so a filename inside an archive is
searchable even when the member itself cannot be parsed.
"""

from __future__ import annotations

import io
import os
import zipfile

from . import ExtractedText, Limits, SubItem, register


def _member_name(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


@register(["zip", "jar", "epub", "kmz"], ["application/zip"], "archive")
def extract_zip(name: str, data: bytes, limits: Limits) -> ExtractedText:
    listing: list[str] = []
    subitems: list[SubItem] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        return ExtractedText(reason=f"bad zip container: {exc}")

    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member = _member_name(info.filename)
            listing.append(member.replace("_", " ").replace("-", " "))
            if len(subitems) >= limits.max_subitems:
                continue
            if info.file_size > limits.max_zip_member_bytes:
                continue
            try:
                payload = archive.read(info)
            except Exception:  # noqa: BLE001 - encrypted or corrupt member
                continue
            subitems.append(SubItem(name=member, mime="", data=payload, kind="archive-member"))

    body = "Members: " + " ".join(listing) if listing else ""
    if not subitems and not listing:
        return ExtractedText(reason="archive was empty")
    return ExtractedText(body=body, subitems=subitems)
