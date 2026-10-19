"""Application service - configured roots, indexing, watching and queries.

Owns the index and the background worker thread. The HTTP layer calls into this
and nothing above it touches SQLite or the filesystem directly, so the transport
could be replaced (named pipes, a native shell) without changing behaviour.

One worker at a time, by design: the index has a single writer, and a second
concurrent crawl would only contend with the first.
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import threading
import time

from pipeline import Builder
from pipeline.extract import Limits
from pipeline.index import Index
from pipeline.watch import Watcher

MARK_OPEN = "\x01"
MARK_CLOSE = "\x02"

# How much text the preview sends. Anything up to this is sent whole, so every
# match can be stepped through instead of only the ones inside an arbitrary
# window. The largest body in a real mailbox here was 721k characters; only a
# handful exceeded 400k. Oversized documents are still windowed around the first
# hit, so the preview never looks like the term was absent.
PREVIEW_MAX_CHARS = 1_000_000


def highlight(text: str) -> str:
    """Escape indexed content and turn match markers into <mark>.

    Order matters: escape first, then substitute. Indexed content is untrusted,
    so nothing from a document may reach the browser as markup.
    """
    escaped = html.escape(text or "")
    return escaped.replace(MARK_OPEN, "<mark>").replace(MARK_CLOSE, "</mark>")


class BusyError(RuntimeError):
    """Raised when a second indexing or watching job is requested."""


class AppService:
    def __init__(self, db_path: str, config_path: str, verbose: bool = False):
        self.index = Index(db_path)
        self.config_path = config_path
        self.verbose = verbose
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._mode = "idle"            # idle | indexing | watching
        self._last_error: str | None = None
        self._progress: dict = {}
        self.resume_watch = False
        self.config = self._load_config()

    # ------------------------------------------------------------------ config

    def _load_config(self) -> dict:
        default = {"roots": [], "watch": {"enabled": False, "interval": 2.0}}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, encoding="utf-8") as handle:
                    data = json.load(handle)
                data.setdefault("roots", [])
                data.setdefault("watch", dict(default["watch"]))
                return data
            except (OSError, ValueError):
                pass
        return default

    def _save_config(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.config_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = self.config_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.config, handle, indent=2)
        os.replace(temporary, self.config_path)

    # ------------------------------------------------------------------- roots

    def roots(self) -> list[dict]:
        return self.config["roots"]

    def active_roots(self) -> list[str]:
        return [r["path"] for r in self.config["roots"] if r.get("enabled", True)]

    def all_root_paths(self) -> list[str]:
        """Every configured root, including unticked ones."""
        return [r["path"] for r in self.config["roots"]]

    def add_root(self, path: str) -> list[dict]:
        path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
        if not os.path.isdir(path):
            raise ValueError(f"not a directory: {path}")
        if any(entry["path"] == path for entry in self.config["roots"]):
            return {"roots": self.config["roots"], "already_present": True}
        self.config["roots"].append({"path": path, "enabled": True})
        self._save_config()
        # Adding a folder should start indexing it. Leaving that as a separate
        # step the user has to discover is how a folder ends up configured but
        # empty. While watching, the watcher adopts the new root next pass.
        result: dict = {"roots": self.config["roots"]}
        if self._mode == "watching":
            result["indexing"] = "the watcher will index it within one scan"
        else:
            try:
                self.start_index()
                result["indexing"] = "started"
            except (BusyError, ValueError) as exc:
                result["indexing"] = f"not started: {exc}"
        return result

    def remove_root(self, path: str) -> dict:
        target = os.path.abspath(path)
        self.config["roots"] = [
            entry for entry in self.config["roots"] if entry["path"] != target
        ]
        self._save_config()
        # Removing a folder must remove what it contributed, or the folder stays
        # searchable after the user removed it.
        removed = self.index.delete_under(target)
        return {"roots": self.config["roots"], "removed_items": removed}

    def clear_index(self) -> dict:
        removed = self.index.clear()
        return {"removed_items": removed, "totals": self.index.stats()}

    def set_root_enabled(self, path: str, enabled: bool) -> list[dict]:
        target = os.path.abspath(path)
        for entry in self.config["roots"]:
            if entry["path"] == target:
                entry["enabled"] = bool(enabled)
        self._save_config()
        return self.config["roots"]

    # --------------------------------------------------------- background work

    def _claim(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise BusyError("an indexing or watching job is already running")
        self.stop_event = threading.Event()

    def _builder(self) -> Builder:
        """A builder whose sweep protects every configured folder."""
        return Builder(
            self.index,
            Limits(),
            self.verbose,
            keep_roots=self.all_root_paths(),
            should_stop=self.stop_event.is_set,
        )

    def is_busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_index(self) -> dict:
        """One-shot reconcile across every enabled root."""
        self._claim()
        if not self.active_roots():
            raise ValueError("no enabled roots configured")
        self._mode = "indexing"
        self._thread = threading.Thread(target=self._run_index, daemon=True)
        self._thread.start()
        return self.status()

    def _run_index(self) -> None:
        self._last_error = None
        try:
            builder = self._builder()
            started = time.perf_counter()
            builder.build_roots(self.active_roots())
            self._progress = {
                "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "elapsed_s": round(time.perf_counter() - started, 2),
                "stopped": bool(builder.stats.get("stopped")),
                "stats": dict(builder.stats),
            }
        except Exception as exc:  # noqa: BLE001 - reported to the UI, not raised
            self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._mode = "idle"

    def start_watch(self, interval: float | None = None, quiet: bool = False) -> dict:
        """Reconcile once, then keep applying changes until stopped."""
        self._claim()
        if not self.active_roots():
            raise ValueError("no enabled roots configured")
        if interval:
            self.config["watch"]["interval"] = float(interval)
        self.config["watch"]["enabled"] = True
        self._save_config()
        self._mode = "watching"
        self._thread = threading.Thread(
            target=self._run_watch, kwargs={"quiet": quiet}, daemon=True
        )
        self._thread.start()
        return self.status()

    def _run_watch(self, quiet: bool = False) -> None:
        # A resume after a mail sync must not erase the error that sync reported,
        # or a failed sync looks like nothing happened at all.
        if not quiet:
            self._last_error = None
        try:
            roots = self.active_roots()
            builder = self._builder()
            started = time.perf_counter()
            builder.build_roots(roots)
            self._progress = {
                "reconciled_s": round(time.perf_counter() - started, 2),
                "stopped": bool(builder.stats.get("stopped")),
                "stats": dict(builder.stats),
            }
            if self.stop_event.is_set():
                return
            watcher = Watcher(
                builder, roots,
                interval=self.config["watch"]["interval"],
                verbose=self.verbose,
                roots_provider=self.active_roots,
            )
            watcher.run(stop_event=self.stop_event)
            if watcher.changes:
                self._progress["recent_changes"] = [
                    {"kind": change.kind, "path": change.path, "outcome": outcome}
                    for change, outcome in watcher.changes[-40:]
                ]
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._mode = "idle"

    # ------------------------------------------------------------------- mail

    def start_mail(
        self,
        limit: int | None = None,
        folder: str | None = None,
        via: str = "file",
    ) -> dict:
        """Sync Outlook mail. Runs on the worker thread like any other job.

        Deliberately not part of the watch loop: mail comes from a store rather
        than the filesystem, and polling a live mailbox on every scan would be
        both slow and rude.

        A running watch is paused first and resumed afterwards. Both write to the
        same index, and the watcher otherwise holds the single worker slot
        forever - which made this unusable in the default state.
        """
        self.resume_watch = self._mode == "watching"
        if self.is_busy():
            if not self.resume_watch:
                raise BusyError("an indexing job is already running")
            self.stop(persist=False)
        self._claim()
        self._mode = "indexing"
        self._thread = threading.Thread(
            target=self._run_mail,
            kwargs={"limit": limit, "folder": folder, "via": via},
            daemon=True,
        )
        self._thread.start()
        return self.status()

    def _run_mail(self, limit: int | None, folder: str | None, via: str = "file") -> None:
        self._last_error = None
        try:
            builder = self._builder()
            started = time.perf_counter()
            counters: dict = {}
            if via == "com":
                # Through Outlook: slower and Outlook must be running, but this
                # is the only route that records each item's entry id, which is
                # what opening a message in Outlook needs.
                import pythoncom

                from pipeline.mail_outlook import iter_messages

                pythoncom.CoInitialize()
                try:
                    builder.build_mail(
                        iter_messages(
                            folder=folder,
                            limit=limit,
                            stats=counters,
                            should_stop=self.stop_event.is_set,
                        ),
                        sweep=limit is None,
                    )
                finally:
                    pythoncom.CoUninitialize()
            else:
                from pipeline.mail_pst import discover_stores, iter_messages

                stores = discover_stores()
                if not stores:
                    raise RuntimeError("no .pst or .ost found in the usual locations")
                builder.build_mail(
                    iter_messages(
                        stores[0],
                        folder=folder,
                        limit=limit,
                        stats=counters,
                        should_stop=self.stop_event.is_set,
                    ),
                    sweep=limit is None,
                )
            self._progress = {
                "mail_via": "Outlook" if via == "com" else "store file",
                "mail_sync_s": round(time.perf_counter() - started, 1),
                "mail_limited": limit is not None,
                "mail_read": counters.get("emitted", 0),
                "mail_skipped": counters.get("skipped", 0),
                "stats": dict(builder.stats),
            }
            # A pass that could not read everything must not look complete.
            if counters.get("skipped"):
                self._last_error = (
                    f"{counters['skipped']} mail item(s) could not be read "
                    f"({counters.get('last_error')}). Mail indexed so far is partial."
                )
            if counters.get("stopped") or self.stop_event.is_set():
                self._progress["stopped"] = True
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._mode = "idle"
            if self.resume_watch:
                self.resume_watch = False
                # Resuming cannot happen on this thread: it is the worker thread,
                # so `is_busy()` would still be true and start_watch would refuse.
                threading.Thread(target=self._resume_watch_after, daemon=True).start()

    def _resume_watch_after(self) -> None:
        for _ in range(100):
            if not self.is_busy():
                break
            time.sleep(0.1)
        try:
            self.start_watch(quiet=True)
        except (BusyError, ValueError):
            pass

    def stop(self, persist: bool = True, wait: float = 5.0) -> dict:
        self.stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=wait)
        if self.is_busy():
            # Keep reporting the job as running until the worker actually
            # returns. Closing the database underneath it would corrupt a write.
            self._mode = "stopping"
        else:
            self._mode = "idle"
        if persist and self.config["watch"].get("enabled"):
            self.config["watch"]["enabled"] = False
            self._save_config()
        return self.status()

    # ---------------------------------------------------------------- queries

    def status(self) -> dict:
        return {
            "mode": self._mode,
            "busy": self.is_busy(),
            "stopping": self.stop_event.is_set() and self.is_busy(),
            "error": self._last_error,
            "progress": self._progress,
            "roots": self.config["roots"],
            "watch": self.config["watch"],
            "totals": self.index.stats(),
            "scopes": self.index.scope_counts(),
            "recent": self.config.get("recent_searches", []),
        }

    def search(self, query: str, limit: int = 30, scope: str = "all") -> dict:
        found = self.index.search_and_match(
            query, limit=limit, scope=scope, markers=(MARK_OPEN, MARK_CLOSE)
        )
        rows, strategy, note = found["rows"], found["strategy"], found["note"]
        return {
            "query": query,
            "count": len(rows),
            "strategy": strategy,
            "note": note,
            "match": found["match"],
            "scope": scope,
            "results": [
                {
                    "id": row["id"],
                    "name_html": highlight(row["name_hl"] or row["name"]),
                    "uri": row["uri"],
                    "kind": row["kind"],
                    "state": row["state"],
                    "reason": row["reason"],
                    "parent_name": row["parent_name"],
                    "attachments": row["attachment_count"],
                    "inline": row["inline_count"],
                    "snippet_html": highlight(row["body_snip"]),
                    "score": row["score"],
                }
                for row in rows
            ],
        }

    def record_search(self, query: str) -> list[str]:
        """Remember a submitted search, newest first, for the recent list."""
        query = (query or "").strip()
        if not query:
            return self.config.get("recent_searches", [])
        recent = [
            entry for entry in self.config.get("recent_searches", [])
            if entry.lower() != query.lower()
        ]
        recent.insert(0, query)
        self.config["recent_searches"] = recent[:10]
        self._save_config()
        return self.config["recent_searches"]

    def item(self, item_id: int, query: str | None = None) -> dict | None:
        row = self.index.get_item_by_id(item_id)
        if row is None:
            return None
        parent = None
        if row["parent_id"] is not None:
            parent_row = self.index.get_item_by_id(int(row["parent_id"]))
            if parent_row is not None:
                parent = {"id": parent_row["id"], "name": parent_row["name"]}

        text = self.index.get_body(item_id, chars=PREVIEW_MAX_CHARS)
        text_html = html.escape(text)
        matches = 0
        shown = 0
        if query:
            # `query` is the expression that matched, not necessarily what the
            # user typed: re-running the raw text can match nothing while the
            # result list is full, and then nothing would highlight.
            marked = self.index.highlighted_body(item_id, query, (MARK_OPEN, MARK_CLOSE))
            if marked:
                matches = marked.count(MARK_OPEN)
                if matches:
                    if len(marked) <= PREVIEW_MAX_CHARS:
                        text_html = highlight(marked)
                        shown = matches
                    else:
                        window = self._window_on_first_match(marked, PREVIEW_MAX_CHARS)
                        shown = window.count(MARK_OPEN)
                        text_html = highlight(window)
                    text = ""
        return {
            "id": row["id"],
            "name": row["name"],
            "uri": row["uri"],
            "kind": row["kind"],
            "state": row["state"],
            "reason": row["reason"],
            "parent": parent,
            "children": [
                {
                    "id": child["id"],
                    "name": child["name"],
                    "kind": child["kind"],
                    "state": child["state"],
                    "reason": child["reason"],
                    "searchable": bool(child["has_text"]),
                }
                for child in self.index.children(item_id)
            ],
            "fields": [
                {"name": field["name"], "value": field["value"]}
                for field in self.index.get_fields(item_id)
            ],
            "text": text,
            "text_html": text_html,
            "matches": matches,
            "shown": shown,
        }

    @staticmethod
    def _window_on_first_match(marked: str, limit: int) -> str:
        first = marked.find(MARK_OPEN)
        if first < 0 or first <= limit - 400:
            return marked[:limit]
        start = max(0, first - 600)
        return "…\n" + marked[start:start + limit]

    # ------------------------------------------------- opening what was found

    def _cache_dir(self) -> str:
        """Where attachments are materialised so they can be opened."""
        # Beside the index rather than the settings file, so it sits with the
        # other generated data.
        index_dir = os.path.dirname(os.path.abspath(self.index.path))
        path = os.path.join(os.path.dirname(index_dir), "attachments")
        os.makedirs(path, exist_ok=True)
        return path

    def _under_a_root(self, path: str) -> bool:
        candidate = os.path.normcase(os.path.abspath(path))
        for root in self.config["roots"]:
            base = os.path.normcase(os.path.abspath(root["path"]))
            if candidate == base or candidate.startswith(base + os.sep):
                return True
        return False

    @staticmethod
    def _leaf_name(uri: str) -> str:
        """The name an item's uri ends at, which is what names its format."""
        if "!/" in uri:
            return uri.partition("!/")[2].split("#")[0]
        return os.path.basename(uri)

    def _member_bytes(self, uri: str, limit_bytes: int = 64 * 1024 * 1024):
        """The bytes an item's uri points at: a whole file, or one member.

        Nothing is retained from indexing, so a container is read again and the
        member extracted on demand. Nested containers recurse - each step returns
        the bytes the next step needs, and the wanted name comes from the current
        uri rather than the parent's.
        """
        from pipeline.extract import Limits, extract_bytes

        if "!/" not in uri:
            if not os.path.exists(uri):
                return None
            with open(uri, "rb") as handle:
                return handle.read(limit_bytes + 1)

        parent, _, member = uri.partition("!/")
        container = self._member_bytes(parent, limit_bytes)
        if container is None:
            return None
        wanted = member.split("#")[0]
        result = extract_bytes(self._leaf_name(parent), container, Limits())
        for sub in result.subitems:
            if sub.name == wanted:
                return sub.data
        return None

    def resolve_path(self, item_id: int) -> str | None:
        """A real filesystem path for an item, extracting a member if needed."""
        row = self.index.get_item_by_id(item_id)
        if row is None:
            raise ValueError("no such item")
        uri = row["uri"]
        if "!/" not in uri:
            return uri if os.path.exists(uri) else None
        data = self._member_bytes(uri)
        if data is None:
            return None
        name = uri.partition("!/")[2].split("#")[0] or "attachment"
        keep = "".join(c for c in name if c not in '<>:"/\\|?*').strip()[:120]
        target = os.path.join(self._cache_dir(), keep or "attachment")
        with open(target, "wb") as handle:
            handle.write(data)
        return target

    def raw_bytes(self, item_id: int, limit_bytes: int = 64 * 1024 * 1024):
        """The item's own bytes plus its name, for an in-browser viewer."""
        row = self.index.get_item_by_id(item_id)
        if row is None:
            raise ValueError("no such item")
        try:
            path = self.resolve_path(item_id)
        except ValueError:
            path = None
        if not path and row["source"] != "files" and (row["origin"] or ""):
            # Mail attachments live in the store, not on disk: Outlook has to
            # write a copy out before anything can read it.
              try:
                  path = self._outlook_save(row)
              except Exception as exc:  # noqa: BLE001 - COM errors are opaque
                  raise ValueError(
                      f"Outlook could not save this attachment for preview: {exc}"
                  ) from exc
        if not path:
            raise ValueError("the underlying file is no longer on disk")
        with open(path, "rb") as handle:
            return handle.read(limit_bytes), os.path.basename(path)

    def _outlook_save(self, row) -> str:
        """Ask Outlook to save one attachment into the cache and return its path."""
        import pythoncom
        import win32com.client

        entry_id = row["uri"].partition("!/")[0].rsplit("/", 1)[-1]
        store_id, _, index = (row["origin"] or "").partition("|")
        if not index:
            raise ValueError("that is a message, not an attachment")
        pythoncom.CoInitialize()
        try:
            namespace = (
                win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
            )
            attachment = namespace.GetItemFromID(entry_id, store_id).Attachments.Item(int(index))
            name = str(attachment.FileName or "attachment")
            keep = "".join(c for c in name if c not in '<>:"/\\|?*').strip()[:120]
            target = os.path.join(self._cache_dir(), keep or "attachment")
            attachment.SaveAsFile(target)
            return target
        finally:
            pythoncom.CoUninitialize()

    def open_item(self, item_id: int, action: str = "open") -> dict:
        """Open or reveal an item. `check` resolves the path without launching."""
        row = self.index.get_item_by_id(item_id)
        if row is None:
            raise ValueError("no such item")
        if row["source"] != "files":
            return self._open_outlook(row, action)

        path = self.resolve_path(item_id)
        if not path:
            raise ValueError("the underlying file is no longer on disk")
        path = os.path.abspath(path)
        inside_cache = path.startswith(os.path.abspath(self._cache_dir()))
        if not self._under_a_root(path) and not inside_cache:
            raise ValueError("refusing to open a path outside the configured folders")

        if action == "check":
            return {"path": path, "action": action}
        if action == "reveal":
            subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            os.startfile(path)          # Windows: whatever owns the file type
        return {"path": path, "action": action}

    def _open_outlook(self, row, action: str) -> dict:
        """Open a mail item, or save one of its attachments, through Outlook.

        Items indexed from a store *file* carry no entry id - libpff's message
        records have no PR_ENTRYID - so this only works for mail that was synced
        through Outlook itself, which records the entry id in the uri and the
        store id in `origin`.
        """
        if not (row["origin"] or ""):
            raise ValueError(
                "this was indexed from the store file, which does not record "
                "Outlook's item id. Run “Sync via Outlook” to make these openable."
            )

        import pythoncom
        import win32com.client

        entry_id = row["uri"].partition("!/")[0].rsplit("/", 1)[-1]
        store_id, _, index = (row["origin"] or "").partition("|")
        pythoncom.CoInitialize()
        try:
            namespace = (
                win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
            )
            item = namespace.GetItemFromID(entry_id, store_id)

            if index:
                if action == "check":
                    name = str(item.Attachments.Item(int(index)).FileName or "attachment")
                    return {"path": name, "action": action}
                target = self._outlook_save(row)
                if action == "reveal":
                    subprocess.Popen(["explorer", f"/select,{target}"])
                else:
                    os.startfile(target)
                return {"path": target, "action": action}

            subject = str(item.Subject)
            if action == "check":
                return {"path": subject, "action": action}
            item.Display()
            return {"path": subject, "action": action}
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - COM raises many shapes
            raise ValueError(
                f"Outlook could not open it ({type(exc).__name__}: {exc}). "
                "It may have been moved or deleted since the last sync."
            ) from exc
        finally:
            pythoncom.CoUninitialize()

    def verify(self) -> dict:
        return self.index.verify()

    def close(self) -> None:
        self.stop(wait=3.0)
        if self.is_busy():
            # This only happens during quit, when the process is about to exit.
            # Leaving the connection open is safer than closing it while the
            # daemon worker is still writing; SQLite's WAL recovers next start.
            return
        self.index.close()
