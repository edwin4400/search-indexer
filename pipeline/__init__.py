"""Pipeline: crawl -> extract -> analyze -> index, with attachment recursion.

The one-shot path came first; incremental correctness was added on top of it:

  * `index_path` / `remove_path` refresh one file, so a change costs one file
    instead of a full crawl.
  * Text fingerprinting - a file whose bytes changed but whose extracted text
    did not is recognised and skipped without touching the index at all.
  * Move detection - a file that reappears at a new path with the same
    extracted text keeps its identity rather than being deleted and reinserted.

The shape mirrors the architecture sketch: one long-running worker owns the
catalog and the index; extraction stays behind `extract_bytes` so an
out-of-process sandbox can replace it without touching the rest of the pipeline.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from collections.abc import Callable

from .crawl import ItemRef, iso_utc, walk
from .analyze import scrub
from .extract import ExtractedText, Limits, SubItem, extract_bytes, scrub_extracted
from .index import Index

# Stored text is capped separately from indexed text: previews need a slice,
# search needs the terms.
PREVIEW_STORE_CHARS = 50_000

# Outcomes of one indexing attempt. The watcher uses these to decide whether a
# change has been applied or should be retried on the next pass.
INDEXED = "indexed"
UPDATED = "updated"
UNCHANGED = "unchanged"
TEXT_UNCHANGED = "text_unchanged"
MOVED = "moved"
SKIPPED = "skipped"
RETRY = "retry"


def text_fingerprint(body: str, fields: dict[str, str]) -> str:
    """Content identity of *extracted* text, independent of container bytes.

    A .docx re-saved by a sync client gets new bytes - zip timestamps and MIME
    boundaries differ - but the same text. Hashing the text rather than the file
    is what lets the index skip that rewrite instead of reindexing it.
    """
    digest = hashlib.sha256()
    digest.update((body or "").encode("utf-8", "replace"))
    for key in sorted(fields or {}):
        digest.update(b"\x00")
        digest.update(key.encode("utf-8", "replace"))
        digest.update(b"=")
        digest.update(str(fields[key]).encode("utf-8", "replace"))
    return digest.hexdigest()


class Builder:
    def __init__(
        self,
        index: Index,
        limits: Limits | None = None,
        verbose: bool = False,
        keep_roots: list[str] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ):
        self.index = index
        self.limits = limits or Limits()
        self.verbose = verbose
        # Roots whose items must survive a sweep even when they are not part of
        # this pass: a folder removed from the UI is gone from this list, a
        # folder merely unticked is not.
        self.keep_roots = list(keep_roots) if keep_roots is not None else None
        self.should_stop = should_stop
        self.stats: Counter[str] = Counter()

    def log(self, *parts) -> None:
        if self.verbose:
            print(*parts, flush=True)

    def _stop_requested(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    # ------------------------------------------------------------- batch build

    def build_roots(self, roots) -> Counter:
        """Reconcile several roots as a single pass.

        The sweep runs once over the union of the walked roots. Running it per
        root deletes every item belonging to the other roots, because their
        files were never walked in this pass - a data-loss bug that only shows
        up once more than one folder is configured.
        """
        roots = list(roots)
        seen: set[str] = set()
        stopped = False
        for root in roots:
            for ref in walk(root, compute_hash=False):
                if self._stop_requested():
                    stopped = True
                    break
                seen.add(ref.uri)
                self._index_top_level(ref, retry_partial=True)
            if stopped:
                break
        if stopped:
            # A partial walk cannot be swept: files that were not reached would
            # look deleted and be removed from the index.
            self.stats["stopped"] += 1
            return self.stats
        keep = self.keep_roots if self.keep_roots is not None else roots
        removed = self.index.sweep(seen, roots, keep)
        if removed:
            self.stats["deleted"] += removed
        return self.stats

    def build_root(self, root: str) -> Counter:
        """Reconcile one root."""
        return self.build_roots([root])

    # -------------------------------------------------------------- mail

    def build_mail(self, records, source: str = "outlook", sweep: bool = True) -> Counter:
        """Index messages from a non-filesystem source.

        Mail is not a directory tree, so it does not go through `walk`. Records
        land in the same catalog and index as files, with a synthetic uri, and
        their attachments go through the ordinary sub-item path.

        *sweep* must be False for a limited sync: a partial pass cannot know
        which messages were deleted, and sweeping would remove the rest.
        """
        seen: set[str] = set()
        stopped = False
        for record in records:
            if self._stop_requested():
                stopped = True
                break
            uri = scrub(record.get("uri") or "")
            seen.add(uri)
            try:
                self._index_mail_record(record, source)
            except Exception as exc:  # noqa: BLE001
                # One unreadable message must not abandon the whole mailbox -
                # which is exactly what a mis-decoded body used to do.
                self.stats["failed"] += 1
                self.log(f"    ! {uri}: {type(exc).__name__}: {exc}")
        if stopped:
            self.stats["stopped"] += 1
            return self.stats
        if sweep:
            removed = self.index.sweep_source(seen, source)
            if removed:
                self.stats["deleted"] += removed
        return self.stats

    def _index_mail_record(self, record: dict, source: str) -> None:
        uri = scrub(record.get("uri") or "")
        result = ExtractedText(
            body=record.get("body") or "",
            fields=dict(record.get("fields") or {}),
            subitems=[
                SubItem(
                    name=item["name"],
                    mime=item.get("mime", ""),
                    data=item.get("data") or b"",
                    origin=item.get("origin", ""),
                )
                for item in (record.get("attachments") or [])
            ],
        )
        # Attachment names join the message's own indexed text, so searching a
        # filename finds the email that carried it as well as the attachment.
        attachment_names = [item.name for item in result.subitems if item.name]
        if attachment_names:
            result.fields["attachments"] = " ".join(attachment_names)
        scrub_extracted(result)
        fingerprint = text_fingerprint(result.body, result.fields)
        previous = self.index.get_item(uri, source)

        if (
            previous is not None
            and previous["state"] in ("indexed", "partial")
            and previous["text_hash"] == fingerprint
        ):
            # Same message, re-read: refresh the timestamp only. Attachments are
            # not walked again, which is what keeps a repeated sync cheap.
            self.index.touch_item(
                int(previous["id"]), previous["size_bytes"], record.get("mtime"),
                None, fingerprint, state="indexed", reason=None,
            )
            self.stats[UNCHANGED] += 1
            return

        ref = ItemRef(
            uri=uri,
            name=scrub(record.get("name") or "") or "(no subject)",
            size=len(result.body.encode("utf-8", "replace")),
            mtime=record.get("mtime"),
            source=source,
            origin=record.get("origin"),
        )
        self._store_extracted(ref, result, parent_id=None, kind="message", depth=0,
                              text_hash=fingerprint)
        self.stats[UPDATED if previous is not None else INDEXED] += 1

    # ------------------------------------------------------- incremental paths

    def index_path(self, path: str) -> str:
        """Index or refresh exactly one top-level path."""
        try:
            stat = os.stat(path)
        except OSError:
            return self.remove_path(path)
        ref = ItemRef(
            uri=os.path.abspath(path),
            name=os.path.basename(path),
            size=stat.st_size,
            mtime=iso_utc(stat.st_mtime),
        )
        return self._index_top_level(ref)

    def remove_path(self, path: str) -> str:
        """Remove one top-level path and every descendant."""
        uri = os.path.abspath(path)
        removed = self.index.delete_by_uri(uri)
        if not removed:
            return UNCHANGED
        self.stats["deleted"] += removed
        self.log(f"  - {os.path.basename(uri)} (+{removed - 1} descendant(s))")
        return "deleted"

    # ------------------------------------------------------------- single file

    def _index_top_level(self, ref: ItemRef, retry_partial: bool = False) -> str:
        previous = self.index.get_item(ref.uri)
        known = previous is not None and previous["state"] in ("indexed", "partial")
        # Re-attempt a file that previously could not be extracted. The set of
        # extractors changes - a new format, or the text sniffer - and without
        # this a file skipped once would stay skipped forever.
        stale_attempt = (
            retry_partial and previous is not None and previous["state"] == "partial"
        )

        # Tier 1 - the cheapest gate. Nothing is read and nothing is hashed:
        # stat alone says this file is exactly as we last recorded it.
        if (
            known
            and not stale_attempt
            and previous["size_bytes"] == ref.size
            and previous["modified_utc"] == ref.mtime
        ):
            self.stats[UNCHANGED] += 1
            return UNCHANGED

        try:
            with open(ref.uri, "rb") as handle:
                data = handle.read(self.limits.max_doc_bytes + 1)
        except OSError as exc:
            # A file being written right now looks like this. Report it as
            # retryable so the watcher keeps the old signature and comes back.
            self.index.mark_state(ref.uri, "failed", f"read error: {exc}", ref)
            self.stats["failed"] += 1
            self.log(f"  ! {ref.name}: {exc}")
            return RETRY

        if len(data) > self.limits.max_doc_bytes:
            self.index.mark_state(
                ref.uri,
                "skipped",
                f"{ref.size} bytes exceeds max_doc_bytes ({self.limits.max_doc_bytes})",
                ref,
            )
            self.stats[SKIPPED] += 1
            return SKIPPED

        ref.content_hash = hashlib.sha256(data).hexdigest()
        result = extract_bytes(ref.name, data, self.limits)
        fingerprint = text_fingerprint(result.body, result.fields)

        # Tier 2 - the bytes changed, the extracted text did not.
        if known and previous["text_hash"] == fingerprint:
            state = "indexed" if result.reason is None else "partial"
            self.index.touch_item(
                int(previous["id"]), ref.size, ref.mtime, ref.content_hash, fingerprint,
                state=state, reason=result.reason,
            )
            self.stats[TEXT_UNCHANGED] += 1
            self.log(f"  = {ref.name} (text unchanged, index untouched)")
            return TEXT_UNCHANGED

        if previous is None:
            outcome = MOVED if self._try_repoint(ref, fingerprint) else INDEXED
        else:
            outcome = UPDATED
        self.log(("  + " if outcome == INDEXED else "  ~ ") + ref.name)

        self._store_extracted(
            ref, result, parent_id=None, kind="file", depth=0, text_hash=fingerprint
        )
        self.stats[outcome] += 1
        return outcome

    def _try_repoint(self, ref: ItemRef, fingerprint: str) -> bool:
        """Recognise a move: same extracted text, old path gone."""
        if not fingerprint:
            return False
        candidates = [
            row
            for row in self.index.find_top_level_by_text_hash(fingerprint)
            if row["uri"] != ref.uri and not os.path.exists(row["uri"])
        ]
        if len(candidates) != 1:
            return False
        self.index.migrate_item(int(candidates[0]["id"]), ref.uri, ref.name)
        self.log(f"    moved from {os.path.basename(candidates[0]['uri'])}")
        return True

    def _store_extracted(
        self,
        ref: ItemRef,
        result,
        parent_id: int | None,
        kind: str,
        depth: int,
        text_hash: str,
    ) -> int:
        state = "indexed" if result.reason is None else "partial"
        item_id = self.index.upsert_item(
            uri=ref.uri,
            name=ref.name,
            parent_id=parent_id,
            size_bytes=ref.size,
            modified_utc=ref.mtime,
            content_hash=ref.content_hash,
            state=state,
            reason=result.reason,
            kind=kind,
            text_hash=text_hash,
            source=ref.source,
            origin=ref.origin,
        )

        # Re-extraction invalidates this item's previous children.
        self.index.drop_children(item_id)

        meta = " ".join(f"{key} {value}" for key, value in (result.fields or {}).items())
        self.index.put_content(
            item_id,
            ref.name,
            result.body or "",
            meta,
            stored=(result.body or "")[:PREVIEW_STORE_CHARS],
            truncated=bool(result.truncated),
        )
        for key, value in (result.fields or {}).items():
            self.index.add_field(item_id, key, str(value))

        if result.reason:
            self.stats["unextracted"] += 1
            self.log(f"    ! {ref.name}: {result.reason}")

        if depth >= self.limits.max_depth:
            return item_id

        used: set[str] = set()
        for sub in (result.subitems or [])[: self.limits.max_subitems]:
            if self._stop_requested():
                self.stats["stopped"] += 1
                break
            sub_uri = f"{ref.uri}!/{sub.name}"
            suffix = 1
            while sub_uri in used:
                suffix += 1
                sub_uri = f"{ref.uri}!/{sub.name}#{suffix}"
            used.add(sub_uri)

            sub_ref = ItemRef(
                uri=sub_uri,
                name=sub.name,
                size=len(sub.data),
                mtime=ref.mtime,
                content_hash=hashlib.sha256(sub.data).hexdigest(),
                source=ref.source,
                origin=sub.origin or None,
            )
            sub_result = extract_bytes(sub.name, sub.data, self.limits)
            self._store_extracted(
                sub_ref,
                sub_result,
                parent_id=item_id,
                kind=sub.kind,
                depth=depth + 1,
                text_hash=text_fingerprint(sub_result.body, sub_result.fields),
            )
            self.stats[sub.kind] += 1

        return item_id
