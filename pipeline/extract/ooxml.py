"""OOXML (docx / xlsx / pptx) using only the standard library.

An OOXML file is a ZIP of XML parts, so this needs no dependency at all. Legacy
OLE formats (.doc/.xls/.ppt) are a different problem and are reported as
unextractable rather than silently skipped.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from . import ExtractedText, Limits, register


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _paragraph_lines(xml_bytes: bytes) -> list[str]:
    """Collect text per paragraph-ish element, preserving line structure."""
    lines: list[str] = []
    for _, element in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if _local(element.tag) in ("p", "row"):
            parts = [t.text or "" for t in element.iter() if _local(t.tag) == "t"]
            if parts:
                text = "".join(parts).strip()
                if text:
                    lines.append(text)
            element.clear()
    return lines


@register(["docx", "docm", "dotx"], ["application/vnd.openxmlformats-officedocument.wordprocessingml"], "ooxml-word")
def extract_docx(name: str, data: bytes, limits: Limits) -> ExtractedText:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        parts = [n for n in names if n == "word/document.xml"]
        parts += sorted(n for n in names if re.match(r"word/(header|footer)\d*\.xml$", n))
        if not parts:
            return ExtractedText(reason="docx has no word/document.xml part")
        lines: list[str] = []
        for part in parts:
            lines.extend(_paragraph_lines(archive.read(part)))
    body = "\n".join(lines)
    return ExtractedText(body=body) if body.strip() else ExtractedText(reason="docx contained no text")


@register(["xlsx", "xlsm"], ["application/vnd.openxmlformats-officedocument.spreadsheetml"], "ooxml-excel")
def extract_xlsx(name: str, data: bytes, limits: Limits) -> ExtractedText:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for _, element in ET.iterparse(io.BytesIO(archive.read("xl/sharedStrings.xml")), events=("end",)):
                if _local(element.tag) == "si":
                    shared.append("".join(t.text or "" for t in element.iter() if _local(t.tag) == "t"))
                    element.clear()

        lines: list[str] = []
        sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d*\.xml$", n))
        for sheet in sheets:
            for _, element in ET.iterparse(io.BytesIO(archive.read(sheet)), events=("end",)):
                if _local(element.tag) != "row":
                    continue
                values: list[str] = []
                for cell in element:
                    if _local(cell.tag) != "c":
                        continue
                    cell_type = cell.get("t")
                    value_el = next((c for c in cell if _local(c.tag) == "v"), None)
                    text = ""
                    if value_el is not None and value_el.text is not None:
                        if cell_type == "s":
                            try:
                                text = shared[int(value_el.text)]
                            except (ValueError, IndexError):
                                text = ""
                        else:
                            text = value_el.text
                    else:
                        inline = next((c for c in cell if _local(c.tag) == "is"), None)
                        if inline is not None:
                            text = "".join(t.text or "" for t in inline.iter() if _local(t.tag) == "t")
                    if text:
                        values.append(text)
                if values:
                    lines.append(" | ".join(values))
                element.clear()
    body = "\n".join(lines)
    return ExtractedText(body=body) if body.strip() else ExtractedText(reason="spreadsheet contained no text")


@register(["pptx", "pptm", "ppsx"], ["application/vnd.openxmlformats-officedocument.presentationml"], "ooxml-powerpoint")
def extract_pptx(name: str, data: bytes, limits: Limits) -> ExtractedText:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        slides = sorted(
            n for n in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)
        )
        lines: list[str] = []
        for slide in slides:
            lines.extend(_paragraph_lines(archive.read(slide)))
    body = "\n".join(lines)
    return ExtractedText(body=body) if body.strip() else ExtractedText(reason="deck contained no text")
