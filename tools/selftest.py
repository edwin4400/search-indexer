"""Self-test - pipeline and incremental correctness.

Builds a throwaway corpus, then exercises the paths that decide whether the
index stays correct: unchanged runs, edits, same-text rewrites, deletions
(including attachment cascade), renames, retryable reads, and the watcher.

    python tools/selftest.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import io
import hashlib
import os
import shutil
import sys
import threading
import time
import zipfile
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)     # `pipeline`
sys.path.insert(0, HERE)     # `make_sample`

from make_sample import make_docx                                    # noqa: E402
from pipeline import MOVED, RETRY, TEXT_UNCHANGED, UNCHANGED, UPDATED, Builder  # noqa: E402
from pipeline.index import Index                                     # noqa: E402
from pipeline.watch import Watcher                                   # noqa: E402

WORK = os.path.join(ROOT, "testrun")
DB = os.path.join(WORK, "m1.db")
WATCH_DB = os.path.join(WORK, "watch.db")
CORPUS = os.path.join(WORK, "corpus")
WATCHED = os.path.join(WORK, "watched")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    marker = "PASS" if ok else "FAIL"
    print(f"  [{marker}] {name}" + (f"  <- {detail}" if detail and not ok else ""))


def write(relative: str, data) -> str:
    path = os.path.join(CORPUS, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def restamp_zip(data: bytes, date_time) -> bytes:
    """Rebuild a container with different entry timestamps.

    Same text, different bytes - exactly what a sync client or backup tool does
    to an untouched .docx.
    """
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as destination:
        for info in source.infolist():
            entry = zipfile.ZipInfo(info.filename, date_time=date_time)
            entry.compress_type = zipfile.ZIP_DEFLATED
            destination.writestr(entry, source.read(info))
    return out.getvalue()


def build_corpus() -> dict[str, str]:
    paths = {
        "notes": write("notes.txt", "alpha bravo charlie\n"),
        "docx": write("report.docx", make_docx(
            ["Quarterly report", "The amortization schedule is approved."])),
    }
    message = EmailMessage()
    message["From"] = "a@b.test"
    message["To"] = "c@d.test"
    message["Subject"] = "Quote for hardware"
    message.set_content("Body says netterms thirty days.\n")
    message.add_attachment(b"Pricing\nEscrow deposit required.\n",
                           maintype="text", subtype="plain", filename="pricing.txt")
    paths["eml"] = write("mail/vendor-quote.eml", message.as_bytes())

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("brief.txt", "The rhubarb rollout is the codename.\n")
    paths["zip"] = write("archive/deliverables.zip", buffer.getvalue())
    return paths


def hits(index: Index, query: str) -> list:
    # Go through the same path the app uses: the raw primitive raises on FTS
    # syntax, which a test query containing '-' or ':' would otherwise turn into
    # a crashed suite rather than a failed check.
    rows, _strategy, _note = index.search_with_fallback(query, limit=20)
    return rows


def fts_rowid(index: Index, path: str):
    row = index.con.execute(
        "SELECT f.rowid AS rid FROM ft f JOIN items i ON i.id = f.item_id WHERE i.uri = ?",
        (path,),
    ).fetchone()
    return row["rid"] if row else None


def main() -> int:
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    paths = build_corpus()
    index = Index(DB)
    builder = Builder(index)

    # ------------------------------------------------------------------ A: build
    print("\nA. initial index")
    builder.build_root(CORPUS)
    totals = index.stats()
    check("A1 four top-level files catalogued", totals["items_top_level"] == 4, str(totals))
    check("A2 one attachment + one archive member",
          totals["items_attachments"] == 1 and totals["items_archive_members"] == 1, str(totals))
    check("A3 verify clean", index.verify()["ok"], str(index.verify()["checks"]))
    check("A4 attachment content searchable", len(hits(index, "escrow")) == 1)
    check("A5 attachment resolves to its parent",
          (hits(index, "escrow")[0]["parent_name"] or "") == "vendor-quote.eml")
    check("A6 archive member searchable", len(hits(index, "rhubarb")) == 1)
    check("A7 filename searchable", len(hits(index, "name:deliverables")) == 1)

    # -------------------------------------------------------------- B: no-op run
    print("\nB. unchanged tree")
    builder = Builder(index)
    stats = builder.build_root(CORPUS)
    check("B1 every file skipped without being read",
          stats.get("unchanged") == 4 and not stats.get("indexed"), str(stats))

    # ----------------------------------------------------------------- C: edit
    print("\nC. edit a file")
    time.sleep(1.1)
    write("notes.txt", "echo foxtrot\n")
    builder = Builder(index)
    stats = builder.build_root(CORPUS)
    check("C1 edit reported as an update", stats.get(UPDATED) == 1, str(stats))
    check("C2 new term searchable", len(hits(index, "foxtrot")) == 1)
    check("C3 removed term no longer matches", len(hits(index, "alpha")) == 0)

    # ------------------------------------- D: same text, different bytes
    print("\nD. rewrites that change bytes but not text")
    rowid_before = fts_rowid(index, paths["notes"])
    time.sleep(1.1)
    write("notes.txt", "echo foxtrot\n")          # identical bytes, new mtime
    builder = Builder(index)
    stats = builder.build_root(CORPUS)
    check("D1 identical-text text file skipped",
          stats.get(TEXT_UNCHANGED) == 1, str(stats))
    check("D2 index row was not churned", fts_rowid(index, paths["notes"]) == rowid_before)

    original = open(paths["docx"], "rb").read()
    rewritten = restamp_zip(original, (2026, 1, 1, 0, 1, 0))
    check("D3 fixture really changed bytes", rewritten != original)
    time.sleep(1.1)
    os.remove(paths["docx"])
    write("report.docx", rewritten)
    builder = Builder(index)
    stats = builder.build_root(CORPUS)
    check("D4 container rewrite with same text skipped",
          stats.get(TEXT_UNCHANGED) == 1, str(stats))
    check("D5 content hash was still refreshed",
          (index.get_item(paths["docx"]) or {"content_hash": None})["content_hash"]
          == hashlib.sha256(rewritten).hexdigest())

    # ---------------------------------------------- E: delete cascades
    print("\nE. delete a container")
    os.remove(paths["eml"])
    builder = Builder(index)
    builder.build_root(CORPUS)
    check("E1 email row gone", index.get_item(paths["eml"]) is None)
    check("E2 attachment cascade removed", len(hits(index, "escrow")) == 0)
    check("E3 email body gone", len(hits(index, "netterms")) == 0)
    check("E4 verify clean after delete", index.verify()["ok"], str(index.verify()["checks"]))

    # ------------------------------------------------------- F: rename = move
    print("\nF. rename a file")
    id_before = index.get_item(paths["docx"])["id"]
    renamed = os.path.join(CORPUS, "renamed-report.docx")
    os.rename(paths["docx"], renamed)
    builder = Builder(index)
    stats = builder.build_root(CORPUS)
    check("F1 move detected", stats.get(MOVED) == 1, str(stats))
    check("F2 identity preserved", (index.get_item(renamed) or {"id": None})["id"] == id_before)
    check("F3 exactly one result for its content", len(hits(index, "amortization")) == 1)
    check("F4 old path gone", index.get_item(paths["docx"]) is None)
    check("F5 verify clean after rename", index.verify()["ok"], str(index.verify()["checks"]))

    # --------------------------------------------------- G: retryable read
    print("\nG. retryable reads")
    blocker = os.path.join(CORPUS, "blocked.txt")
    os.makedirs(blocker)
    outcome = Builder(index).index_path(blocker)
    check("G1 unreadable path reports retry", outcome == RETRY, outcome)
    check("G2 failure recorded in catalog",
          (index.get_item(blocker) or {"state": ""})["state"] == "failed")
    os.rmdir(blocker)
    Builder(index).build_root(CORPUS)
    check("G3 stale failure swept away", index.get_item(blocker) is None)

    # ------------------------------------------------------------ H: watcher
    print("\nH. watcher")
    os.makedirs(WATCHED, exist_ok=True)
    watch_index = Index(WATCH_DB)
    watcher = Watcher(Builder(watch_index), [WATCHED], interval=0.4)
    thread = threading.Thread(
        target=watcher.run, kwargs={"iterations": 24}, daemon=True)
    thread.start()
    time.sleep(0.9)

    live = os.path.join(WATCHED, "live.txt")
    with open(live, "w", encoding="utf-8") as handle:
        handle.write("golf hotel\n")
    time.sleep(1.3)
    check("H1 created file indexed", len(hits(watch_index, "golf")) == 1)

    with open(live, "w", encoding="utf-8") as handle:
        handle.write("india juliet\n")
    time.sleep(1.3)
    check("H2 modified file reindexed",
          len(hits(watch_index, "juliet")) == 1 and len(hits(watch_index, "golf")) == 0)

    os.remove(live)
    time.sleep(1.3)
    check("H3 deleted file removed", len(hits(watch_index, "juliet")) == 0)

    thread.join(timeout=8)
    check("H4 watcher stopped on iteration limit", not thread.is_alive())
    outcomes = [outcome for _, outcome in watcher.changes]
    check("H5 watcher applied create, update and delete in order",
          outcomes and outcomes[0] == "indexed" and UPDATED in outcomes
          and outcomes[-1] == "deleted", str(watcher.changes))
    check("H6 watched index verifies clean",
          watch_index.verify()["ok"], str(watch_index.verify()["checks"]))

    # ------------------------------------------------------- J: multiple roots
    print("\nJ. several roots at once")
    second = os.path.join(WORK, "second")
    os.makedirs(second, exist_ok=True)
    with open(os.path.join(second, "other.txt"), "w", encoding="utf-8") as handle:
        handle.write("november oscar\n")

    multi = Index(os.path.join(WORK, "multi.db"))
    multi_builder = Builder(multi, keep_roots=[CORPUS, second])
    multi_builder.build_roots([CORPUS, second])
    check("J1 both roots indexed",
          len(hits(multi, "oscar")) == 1 and len(hits(multi, "foxtrot")) == 1)

    # The regression this case exists for: re-indexing one root must not sweep
    # away the other, whose files were not walked during that pass.
    multi_builder.build_roots([CORPUS])
    check("J2 re-indexing one root keeps the other root's items",
          len(hits(multi, "oscar")) == 1)
    check("J3 the re-indexed root is still intact", len(hits(multi, "foxtrot")) == 1)

    removed = multi.delete_under(second)
    check("J4 removing a root purges what it contributed",
          removed >= 1 and len(hits(multi, "oscar")) == 0, f"removed={removed}")
    check("J5 the purge leaves the other root alone", len(hits(multi, "foxtrot")) == 1)
    check("J6 multi-root index verifies clean",
          multi.verify()["ok"], str(multi.verify()["checks"]))
    multi.close()

    # ------------------------------------------------- K: query interpretation
    print("\nK. query handling")
    rows, strategy, _ = index.search_with_fallback("amortization")
    check("K1 a whole word matches exactly",
          strategy == "exact" and len(rows) == 1, f"{strategy}/{len(rows)}")

    # The failure a person actually hits: FTS matches whole tokens, so a partial
    # word finds nothing and used to look like "the search is broken".
    rows, strategy, note = index.search_with_fallback("amortiz")
    check("K2 a partial word is broadened to a prefix match",
          strategy == "prefix" and len(rows) == 1 and bool(note),
          f"{strategy}/{len(rows)}/{note}")

    # A colon makes FTS5 read the word as a column filter and reject the query.
    rows, strategy, _ = index.search_with_fallback("amortization: schedule")
    check("K3 a query with FTS operators is still matched",
          strategy in ("words", "phrase") and len(rows) == 1, f"{strategy}/{len(rows)}")

    rows, strategy, note = index.search_with_fallback("zzznotpresentanywhere")
    check("K4 an absent term reports no match rather than guessing",
          strategy == "none" and len(rows) == 0 and note is None,
          f"{strategy}/{len(rows)}")

    # --------------------------------- L: files no extractor claims
    print("\nL. unknown extensions")
    unknown = os.path.join(CORPUS, "shell-session.out")
    with open(unknown, "w", encoding="utf-8") as handle:
        handle.write("$ ls -la\n$ sudo systemctl restart quuxdaemon\n")
    # An unknown extension, so the byte heuristic - not the extension list - is
    # what has to reject it.
    binary = os.path.join(CORPUS, "looks-binary.unknownblob")
    with open(binary, "wb") as handle:
        handle.write(b"\x00\x01\x02\x03MZ\x90\x00" * 64)

    Builder(index).build_roots([CORPUS])
    check("L1 an unregistered text extension is indexed by its content",
          len(hits(index, "quuxdaemon")) == 1)
    check("L2 its filename is searchable as well",
          len(hits(index, "name:shell")) == 1)
    row = index.get_item(unknown)
    check("L3 sniffed text is indexed, not left partial",
          row is not None and row["state"] == "indexed",
          f"{row['state'] if row else None}/{row['reason'] if row else None}")
    binary_row = index.get_item(binary)
    check("L4 binary content is catalogued without text",
          binary_row is not None and binary_row["state"] == "partial"
          and "not text" in (binary_row["reason"] or ""),
          f"{binary_row['state'] if binary_row else None}/{binary_row['reason'] if binary_row else None}")

    # A file that failed to extract earlier must be retried on a full pass, or a
    # newly supported format would never be picked up. State is set directly so
    # size, mtime and text_hash stay valid and the retry is the only reason it
    # changes.
    index.con.execute(
        "UPDATE items SET state = 'partial', reason = 'simulated earlier failure' "
        "WHERE uri = ?",
        (unknown,),
    )
    index.con.commit()
    outcome = Builder(index).index_path(unknown)
    check("L5 the incremental path leaves an unextracted item alone",
          outcome == UNCHANGED, outcome)
    Builder(index).build_roots([CORPUS])
    check("L6 a full pass retries a previously unextracted file",
          index.get_item(unknown)["state"] == "indexed",
          str(index.get_item(unknown)["reason"]))

    # --------------------------------------------------------------- M: mail
    print("\nM. mail records")
    mail = Index(os.path.join(WORK, "mail.db"))
    mail_builder = Builder(mail)

    def mail_records(include_second=True):
        records = [{
            "uri": "outlook://Inbox/E1",
            "name": "Quote for hardware",
            "mtime": "2026-10-15T09:00:00+00:00",
            "fields": {"from": "Vendor Rep", "subject": "Quote for hardware",
                       "folder": "Inbox"},
            "body": "Payment terms are netterms thirty days.",
            "attachments": [{"name": "pricing-sheet.txt", "mime": "",
                                 "data": b"Escrow deposit required before shipment."},
                                {"name": "Outlook-1.png", "mime": "",
                                 "data": b"\x89PNG\r\n\x1a\n fake"}],
        }]
        if include_second:
            records.append({
                "uri": "outlook://Inbox/E2",
                "name": "Team lunch",
                "mtime": "2026-10-15T10:00:00+00:00",
                "fields": {"from": "Ops", "subject": "Team lunch", "folder": "Inbox"},
                "body": "Booked for friday.",
                "attachments": [],
            })
        return iter(records)

    mail_builder.build_mail(mail_records())
    totals = mail.stats()
    check("M1 messages and their attachment are indexed",
          totals["items_top_level"] == 2 and totals["items_attachments"] == 2, str(totals))
    check("M2 attachment content is searchable", len(hits(mail, "escrow")) == 1)
    rows = hits(mail, "escrow")
    check("M3 attachment resolves to its message",
          bool(rows) and (rows[0]["parent_name"] or "") == "Quote for hardware",
          rows[0]["parent_name"] if rows else "no rows")
    check("M4 message body is searchable", len(hits(mail, "netterms")) == 1)
    check("M5 header fields are searchable", len(hits(mail, "meta:vendor")) == 1)

    stats = mail_builder.build_mail(mail_records())
    check("M6 a repeat sync changes nothing",
          stats.get(UNCHANGED) == 2 and not stats.get("deleted"), str(dict(stats)))

    mail_builder.build_mail(mail_records(include_second=False))
    check("M7 a message gone from Outlook is swept",
          mail.stats()["items_top_level"] == 1 and len(hits(mail, "friday")) == 0,
          str(mail.stats()))
    check("M8 the remaining message keeps its attachment", len(hits(mail, "escrow")) == 1)
    check("M9 mail index verifies clean", mail.verify()["ok"], str(mail.verify()["checks"]))

    # ---------------------------------------- N: text that breaks encoding
    print("\nN. surrogates from a mis-detected encoding")
    bad = Index(os.path.join(WORK, "badtext.db"))
    lone = "subject \udbc0 tail"
    Builder(bad).build_mail(iter([{
        "uri": "outlook://Inbox/BAD",
        "name": "Broken encoding",
        "mtime": None,
        "fields": {"subject": lone},
        "body": "body " + lone,
        "attachments": [{"name": "att-\udbc0.txt", "mime": "", "data": b"\xdb\xc0 note"}],
    }]))
    check("N1 a lone surrogate does not break indexing",
          bad.stats()["items_top_level"] == 1, str(bad.stats()))
    check("N2 the message is still searchable", len(hits(bad, "broken")) == 1)
    item = bad.get_item("outlook://Inbox/BAD", source="outlook")
    body = bad.get_body(int(item["id"])) if item else ""
    check("N3 stored text is present", bool(body.strip()), repr(body[:40]))
    check("N4 no surrogate survives in stored text",
          not any(0xD800 <= ord(ch) <= 0xDFFF for ch in body), repr(body[:40]))
    check("N5 the replacement character is visible", "\ufffd" in body, repr(body[:40]))
    check("N6 the attachment was indexed too",
          bad.stats()["items_attachments"] == 1, str(bad.stats()))
    check("N7 index verifies clean", bad.verify()["ok"], str(bad.verify()["checks"]))
    bad.close()

    odd = os.path.join(CORPUS, "odd-encoding.txt")
    with open(odd, "wb") as handle:
        handle.write(b"caf\xe9 na\xefve \xdb\xc0 weird\n")
    Builder(index).build_roots([CORPUS])
    check("N8 a cp1252 file is not mis-read as utf-16",
          len(hits(index, "weird")) == 1)

    # ------------------------------------------- O: attachment visibility
    print("\nO. seeing attachments")
    mail_builder.build_mail(mail_records())
    rows, _strategy, _note = mail.search_with_fallback("Quote for hardware")
    check("O1 the message reports how many attachments it has",
          bool(rows) and rows[0]["attachment_count"] == 1 and rows[0]["inline_count"] == 1,
          f"attachments={rows[0]['attachment_count']} inline={rows[0]['inline_count']}"
          if rows else "no rows")

    children = mail.children(int(rows[0]["id"]))
    by_name = {child["name"]: child for child in children}
    check("O2 every child is listed under the message",
          set(by_name) == {"pricing-sheet.txt", "Outlook-1.png"}, str(sorted(by_name)))
    check("O3 the listing says which contents are searchable",
          bool(by_name["pricing-sheet.txt"]["has_text"])
          and not bool(by_name["Outlook-1.png"]["has_text"]),
          str({name: bool(child["has_text"]) for name, child in by_name.items()}))

    rows, _strategy, _note = mail.search_with_fallback("pricing-sheet")
    kinds = {row["kind"] for row in rows}
    check("O4 an attachment filename also finds the email", "message" in kinds, str(kinds))
    check("O5 searching inside the attachment still works", len(hits(mail, "escrow")) == 1)
    check("O6 mail index still verifies clean", mail.verify()["ok"], str(mail.verify()["checks"]))

    # ------------------------------------------------ T: scoping the search
    print("\nT. scoping the search")
    counts = mail.scope_counts()
    check("T1 the scope counts are reported",
          set(counts) == {"all", "files", "email", "attachments"} and counts["all"] > 0,
          str(counts))
    found = mail.search_and_match("netterms", scope="email")
    check("T2 an email-scoped search finds the message",
          len(found["rows"]) == 1 and found["rows"][0]["kind"] == "message",
          str([row["kind"] for row in found["rows"]]))
    found = mail.search_and_match("netterms", scope="attachments")
    check("T3 an email term is excluded from the attachment scope",
          len(found["rows"]) == 0, str(len(found["rows"])))
    found = mail.search_and_match("escrow", scope="attachments")
    check("T4 an attachment-scoped search finds the attachment",
          len(found["rows"]) == 1, str(len(found["rows"])))
    found = mail.search_and_match("escrow", scope="email")
    check("T5 an attachment term is excluded from the email scope",
          len(found["rows"]) == 0, str(len(found["rows"])))
    mail.close()

    # -------------------------------------- P: preview highlight window
    print("\nP. preview highlighting")
    preview_db = Index(os.path.join(WORK, "preview.db"))
    filler = "filler words here " * 3200          # ~58k chars, past the 50k store cap
    long_body = filler + "the rate of peroxidation matters. later, peroxidation again."
    Builder(preview_db).build_mail(iter([{
        "uri": "outlook://Inbox/P1",
        "name": "Long attachment",
        "mtime": None,
        "fields": {"subject": "Long attachment"},
        "body": long_body,
        "attachments": [{"name": "long.txt", "mime": "", "data": long_body.encode()}],
    }]))
    rows, _strategy, _note = preview_db.search_with_fallback("peroxidation")
    check("P1 the email and its attachment both match", len(rows) == 2, str(len(rows)))
    attachments = [row for row in rows if row["kind"] == "attachment"]
    check("P2 the attachment match is present", len(attachments) == 1, str(len(attachments)))

    preview_id = int(attachments[0]["id"])
    stored = preview_db.get_body(preview_id, chars=10_000_000)
    check("P3 the stored preview really is truncated before the term",
          "peroxidation" not in stored, f"stored={len(stored)} chars")

    marked = preview_db.highlighted_body(preview_id, "peroxidation", ("\x01", "\x02"))
    check("P4 highlighting recovers the match the stored text lost",
          "\x01peroxidation\x02" in marked, repr(marked[:60]))
    check("P5 every occurrence is marked, not only the first",
          marked.count("\x01") == 2, str(marked.count("\x01")))
    preview_db.close()

    # ----------------------------------------- R: numbers and match reuse
    print("\nR. numbers, and reusing the match expression")
    prices = os.path.join(CORPUS, "prices.txt")
    with open(prices, "w", encoding="utf-8") as handle:
        handle.write("Grand Total (SGD) : 6,540.00\nGST 9% : 540.00\n")
    Builder(index).build_roots([CORPUS])

    found = index.search_and_match("6,540.00")
    check("R1 a number that is present is found",
          len(found["rows"]) >= 1, f"{found['strategy']}/{len(found['rows'])}")

    found = index.search_and_match("$6,450.00")
    check("R2 an absent number is not invented by prefix broadening",
          len(found["rows"]) == 0 and found["strategy"] == "none",
          f"{found['strategy']}/{len(found['rows'])}")
    check("R2b and it says the string was searched whole",
          bool(found["note"]), repr(found["note"]))

    found = index.search_and_match("amortiz")
    check("R3 a partial word still broadens",
          found["strategy"] == "prefix", found["strategy"])
    check("R4 the expression that matched is reported", bool(found["match"]),
          repr(found["match"]))
    if found["rows"]:
        marked = index.highlighted_body(
            int(found["rows"][0]["id"]), found["match"], ("\x01", "\x02")
        )
        check("R5 the preview highlights with that same expression",
              "\x01" in marked, repr(marked[:60]))
    else:
        check("R5 the preview highlights with that same expression", False, "no rows")

    # ----------------------------------------- S: match navigation counts
    print("\nS. stepping through matches")
    from app.service import AppService

    nav_db = os.path.join(WORK, "nav.db")
    wide = ("the rate of peroxidation matters.\n"
            + "filler words here " * 2600          # wider than the preview window
            + "peroxidation again near the end.\n")
    nav_index = Index(nav_db)
    Builder(nav_index).build_mail(iter([{
        "uri": "outlook://Inbox/S1",
        "name": "Wide document",
        "mtime": None,
        "fields": {"subject": "Wide document"},
        "body": wide,
        "attachments": [],
    }]))
    nav_index.close()

    service = AppService(nav_db, os.path.join(WORK, "nav.json"))
    found = service.search("peroxidation")
    check("S1 the wide document matches", found["count"] == 1, str(found["count"]))
    item = service.item(found["results"][0]["id"], query=found["match"])
    check("S2 every occurrence in the document is counted",
          item["matches"] == 2, str(item["matches"]))
    check("S3 every occurrence is steppable, not just those in a window",
          item["shown"] == 2, str(item["shown"]))
    check("S4 the view highlights both",
          item["text_html"].count("<mark>") == 2, item["text_html"].count("<mark>"))
    # A document too large to send whole still keeps its first hit visible.
    window = AppService._window_on_first_match("\x01a\x02" + "x" * 40 + "\x01b\x02", 20)
    check("S5 an oversized document keeps the hit it was opened on",
          "\x01" in window, repr(window[:40]))
    service.close()

    # ---------------------------------------------------------- U: stopping jobs
    print("\nU. stopping a long job")
    stop_db = os.path.join(WORK, "stop.db")
    stop_index = Index(stop_db)
    Builder(stop_index).build_mail(iter([{
        "uri": "outlook://Inbox/A",
        "name": "Already indexed",
        "fields": {},
        "body": "alpha",
        "attachments": [],
    }]))

    stop = threading.Event()

    def stopped_records():
        yield {
            "uri": "outlook://Inbox/B",
            "name": "Reached before stop",
            "fields": {},
            "body": "bravo",
            "attachments": [],
        }
        stop.set()
        yield {
            "uri": "outlook://Inbox/C",
            "name": "Never reached",
            "fields": {},
            "body": "charlie",
            "attachments": [],
        }

    stats = Builder(stop_index, should_stop=stop.is_set).build_mail(
        stopped_records(), sweep=True
    )
    check("U1 the record already being processed is kept",
          stop_index.get_item("outlook://Inbox/B", "outlook") is not None)
    check("U2 a stop skips the deletion sweep",
          stop_index.get_item("outlook://Inbox/A", "outlook") is not None)
    check("U3 records after the stop are not read",
          stop_index.get_item("outlook://Inbox/C", "outlook") is None)
    check("U4 the build reports that it stopped", stats.get("stopped") == 1,
          str(dict(stats)))
    stop_index.close()

    stuck = AppService(
        os.path.join(WORK, "stuck.db"), os.path.join(WORK, "stuck.json")
    )
    stuck._mode = "indexing"
    stuck._thread = threading.Thread(target=lambda: time.sleep(0.8), daemon=True)
    stuck._thread.start()
    started = time.monotonic()
    status = stuck.stop(wait=0.1)
    elapsed = time.monotonic() - started
    check("U5 stop answers while a worker is still finishing",
          elapsed < 0.5 and status["stopping"], f"{elapsed:.2f}s {status['mode']}")
    stuck._thread.join(2)
    stuck.close()

    # ------------------------------------------------------------------ I: final
    print("\nI. final state")
    report = index.verify()
    check("I1 main index verifies clean", report["ok"], str(report["checks"]))
    check("I2 attachment count is zero after the cascade",
          index.stats()["items_attachments"] == 0, str(index.stats()))

    index.close()
    watch_index.close()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    print("self-test PASS")
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # A crash part-way through used to end the run with no summary at all,
        # which hides every check after it.
        passed = sum(1 for _, ok, _ in results if ok)
        print(f"\nFATAL: suite stopped early after {passed}/{len(results)} checks "
              f"- {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
    raise SystemExit(exit_code)
