"""Generate a small demo corpus that exercises every extractor.

Each content file carries one distinctive token so a query can prove exactly
which stage surfaced it:

    kazoo        - plain text body
    parsnip      - markdown body
    amortization - inside a .docx
    consultancy  - inside a .xlsx
    indemnity    - inside a generated PDF
    netterms     - email body          vendor-quote.eml  (filename hit: "vendor")
    escrow       - inside an email ATTACHMENT
    rhubarb      - inside a ZIP MEMBER
"""

from __future__ import annotations

import io
import os
import zipfile
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(os.path.dirname(HERE), "sample")


def write(relative: str, data) -> str:
    path = os.path.join(SAMPLE, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


# --------------------------------------------------------------------- OOXML

def make_docx(paragraphs: list[str]) -> bytes:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def make_xlsx(rows: list[list[str]]) -> bytes:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Budget" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        "</Relationships>"
    )

    shared: list[str] = []
    index_of: dict[str, int] = {}
    rows_xml = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column, value in enumerate(row):
            if value not in index_of:
                index_of[value] = len(shared)
                shared.append(value)
            ref = f"{chr(65 + column)}{row_number}"
            cells.append(f'<c r="{ref}" t="s"><v>{index_of[value]}</v></c>')
        rows_xml.append(f"<row r=\"{row_number}\">{''.join(cells)}</row>")

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows_xml)}</sheetData></worksheet>"
    )
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{value}</t></si>" for value in shared)
        + "</sst>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/sharedStrings.xml", shared_xml)
    return buffer.getvalue()


# ----------------------------------------------------------------------- PDF

def make_pdf(text: str) -> bytes:
    """A minimal, structurally valid single-page PDF (correct xref offsets)."""
    stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


# --------------------------------------------------------------------- build

def main() -> None:
    write("notes/meeting-notes.txt",
          "Weekly sync notes.\nThe kazoo migration is on track for next month.\n")

    write("notes/README-notes.md",
          "# Notes\n\nThe parsnip rollout plan lives in the shared drive.\n")

    write("notes/quarterly-report.docx", make_docx([
        "Quarterly report",
        "The amortization schedule for FY26 has been reviewed.",
        "Operating margin improved slightly against plan.",
    ]))

    write("reports/budget.xlsx", make_xlsx([
        ["Line item", "Amount"],
        ["Software", "12000"],
        ["Consultancy", "45000"],
    ]))

    write("reports/vendor-terms.pdf", make_pdf(
        "Clause 7. The indemnity obligations survive termination of this agreement."))

    message = EmailMessage()
    message["From"] = "Vendor Rep <sales@example-vendor.test>"
    message["To"] = "Edwin <edwin@example.test>"
    message["Subject"] = "Quote for Q3 hardware refresh"
    message.set_content(
        "Hi Edwin,\n\nPlease find pricing attached.\nPayment terms are netterms thirty days.\n\nRegards,\nVendor Rep\n"
    )
    message.add_attachment(
        b"Pricing sheet\nEscrow deposit of 10 percent is required before shipment.\n",
        maintype="text", subtype="plain", filename="pricing-sheet.txt",
    )
    write("inbox/vendor-quote.eml", message.as_bytes())

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("specs/brief.txt",
                         "Project brief\nThe rhubarb rollout is the codename for this programme.\n")
        archive.writestr("specs/plan.md",
                         "# Plan\nMilestones, owners and delivery dates.\n")
    write("archive/deliverables.zip", buffer.getvalue())

    print(f"sample corpus written to {SAMPLE}")


if __name__ == "__main__":
    main()
