"""Stage 4/5 - the catalog and the search index.

Two stores by design:
  * `items` / `bodies` / `fields` - the catalog. Metadata plus stored text for
    preview, so the preview pane never re-opens the source file.
  * `ft` - a SQLite FTS5 virtual table. Term positions, phrase queries, fielded
    columns, BM25 ranking, and highlight()/snippet() all come from FTS5.

The index is rebuildable from the catalog + sources, so catalog writes are the
durable record and `ft` is treated as derived.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    source        TEXT    NOT NULL DEFAULT 'files',
    uri           TEXT    NOT NULL,
    name          TEXT,
    mime          TEXT,
    kind          TEXT    NOT NULL DEFAULT 'file',   -- file | attachment | archive-member
    size_bytes    INTEGER,
    modified_utc  TEXT,
    content_hash  TEXT,
    text_hash     TEXT,                              -- hash of extracted text, not bytes
    origin        TEXT,                              -- identifier the source uses to reopen it
    parent_id     INTEGER REFERENCES items(id),
    state         TEXT    NOT NULL DEFAULT 'indexed', -- indexed | partial | skipped | failed | deleted
    reason        TEXT,
    indexed_utc   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_items_source_uri ON items(source, uri);
CREATE INDEX IF NOT EXISTS ix_items_parent ON items(parent_id);
CREATE INDEX IF NOT EXISTS ix_items_state ON items(state);

CREATE TABLE IF NOT EXISTS bodies (
    item_id   INTEGER PRIMARY KEY REFERENCES items(id),
    text      TEXT,
    truncated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fields (
    item_id INTEGER NOT NULL REFERENCES items(id),
    name    TEXT    NOT NULL,
    value   TEXT
);
CREATE INDEX IF NOT EXISTS ix_fields_item ON fields(item_id);

-- Column 0 is unindexed so rows can be deleted by item id; 1..3 are searchable.
CREATE VIRTUAL TABLE IF NOT EXISTS ft USING fts5(
    item_id UNINDEXED,
    name,
    body,
    meta,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

# Children whose name marks them as an inline image rather than a real
# attachment. Counted separately because a signature graphic is not what anyone
# means by "this email has an attachment".
INLINE_IMAGE_PATTERNS = ("%.png", "%.jpg", "%.jpeg", "%.gif", "%.bmp", "%.tif",
                         "%.tiff", "%.webp", "%.emf", "%.wmf", "%.ico")
_IMAGE_TEST = " OR ".join(f"lower(c.name) LIKE '{pattern}'" for pattern in INLINE_IMAGE_PATTERNS)

# Scope filters, mirroring X1's navigation pane: everything, only files, only
# mail, or only what was attached to something.
SCOPES = {
    "all": "",
    "files": "AND i.source = 'files'",
    "email": "AND i.kind = 'message'",
    "attachments": "AND i.kind IN ('attachment', 'archive-member')",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Index:
    def __init__(self, path: str):
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        # The watcher writes from its own thread while callers read from another,
        # so the connection is shared deliberately. SQLite serialises access
        # internally (sqlite3.threadsafety == 3 on CPython) and WAL mode keeps
        # readers from blocking the single writer. Without this the watcher
        # raises "SQLite objects created in a thread can only be used in that
        # same thread" the moment it applies a change.
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        existing = {row["name"] for row in self.con.execute("PRAGMA table_info(items)")}
        for column, declaration in (("text_hash", "TEXT"), ("origin", "TEXT")):
            if column not in existing:
                self.con.execute(f"ALTER TABLE items ADD COLUMN {column} {declaration}")
        self.con.commit()

    def close(self) -> None:
        self.con.commit()
        self.con.close()

    # ---------------------------------------------------------------- catalog

    def get_item(self, uri: str, source: str = "files"):
        return self.con.execute(
            "SELECT * FROM items WHERE source = ? AND uri = ?", (source, uri)
        ).fetchone()

    def upsert_item(
        self,
        uri: str,
        name: str,
        parent_id: int | None,
        size_bytes: int | None,
        modified_utc: str | None,
        content_hash: str | None,
        state: str,
        reason: str | None = None,
        kind: str = "file",
        source: str = "files",
        text_hash: str | None = None,
        origin: str | None = None,
    ) -> int:
        row = self.con.execute(
            """
            INSERT INTO items (source, uri, name, kind, parent_id, size_bytes,
                               modified_utc, content_hash, text_hash, origin,
                               state, reason, indexed_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, uri) DO UPDATE SET
                name         = excluded.name,
                kind         = excluded.kind,
                parent_id    = excluded.parent_id,
                size_bytes   = excluded.size_bytes,
                modified_utc = excluded.modified_utc,
                content_hash = excluded.content_hash,
                text_hash    = excluded.text_hash,
                origin       = excluded.origin,
                state        = excluded.state,
                reason       = excluded.reason,
                indexed_utc  = excluded.indexed_utc
            RETURNING id
            """,
            (source, uri, name, kind, parent_id, size_bytes, modified_utc,
             content_hash, text_hash, origin, state, reason, _now()),
        ).fetchone()
        self.con.commit()
        return int(row["id"])

    def touch_item(
        self,
        item_id: int,
        size_bytes: int | None,
        modified_utc: str | None,
        content_hash: str | None,
        text_hash: str | None,
        state: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Refresh bookkeeping only - the indexed content is provably unchanged.

        This is the cheap path for files whose bytes changed but whose extracted
        text did not (Office files rewritten by a sync client, for example). No
        FTS delete/insert, no field rewrite, no child churn.

        *state* and *reason* are optional: pass them when a file that previously
        could not be extracted now can, so its state is corrected without
        rewriting content that has not changed.
        """
        if state is None:
            self.con.execute(
                """
                UPDATE items
                   SET size_bytes = ?, modified_utc = ?, content_hash = ?,
                       text_hash = ?, indexed_utc = ?
                 WHERE id = ?
                """,
                (size_bytes, modified_utc, content_hash, text_hash, _now(), item_id),
            )
        else:
            self.con.execute(
                """
                UPDATE items
                   SET size_bytes = ?, modified_utc = ?, content_hash = ?,
                       text_hash = ?, state = ?, reason = ?, indexed_utc = ?
                 WHERE id = ?
                """,
                (size_bytes, modified_utc, content_hash, text_hash, state, reason,
                 _now(), item_id),
            )
        self.con.commit()

    def find_top_level_by_text_hash(self, text_hash: str) -> list[sqlite3.Row]:
        """Candidates for a move: same extracted text, different uri."""
        return self.con.execute(
            """
            SELECT id, uri, name FROM items
             WHERE parent_id IS NULL AND text_hash = ? AND state IN ('indexed', 'partial')
             ORDER BY uri
            """,
            (text_hash,),
        ).fetchall()

    def migrate_item(self, item_id: int, new_uri: str, new_name: str) -> None:
        """Re-point an existing item at a new path, preserving its identity.

        Children are intentionally left alone: the caller re-extracts the new
        path, which drops and rebuilds them from the new parent uri.
        """
        self.con.execute(
            "UPDATE items SET uri = ?, name = ?, indexed_utc = ? WHERE id = ?",
            (new_uri, new_name, _now(), item_id),
        )
        self.con.commit()

    def mark_state(self, uri: str, state: str, reason: str | None, ref=None) -> None:
        self.upsert_item(
            uri=uri,
            name=(ref.name if ref else os.path.basename(uri)),
            parent_id=None,
            size_bytes=(ref.size if ref else None),
            modified_utc=(ref.mtime if ref else None),
            content_hash=(ref.content_hash if ref else None),
            state=state,
            reason=reason,
        )

    def delete_item(self, item_id: int) -> None:
        self.con.execute("DELETE FROM ft WHERE item_id = ?", (item_id,))
        self.con.execute("DELETE FROM fields WHERE item_id = ?", (item_id,))
        self.con.execute("DELETE FROM bodies WHERE item_id = ?", (item_id,))
        self.con.execute("DELETE FROM items WHERE id = ?", (item_id,))

    def drop_children(self, parent_id: int) -> int:
        """Remove every descendant of *parent_id* (attachments, archive members)."""
        removed = 0
        children = self.con.execute(
            "SELECT id FROM items WHERE parent_id = ?", (parent_id,)
        ).fetchall()
        for child in children:
            removed += self.drop_children(int(child["id"]))
            self.delete_item(int(child["id"]))
            removed += 1
        self.con.commit()
        return removed

    def sweep(
        self,
        seen_uris: set[str],
        walked_roots: list[str],
        keep_roots: list[str] | None = None,
        source: str = "files",
    ) -> int:
        """Delete top-level items whose file is gone.

        Sweeping per root is wrong: indexing root A would delete everything
        belonging to root B, because B was not walked during that pass. The
        decision therefore uses both the walked set and the configured roots:

        * in *seen_uris*           -> keep
        * under a walked root      -> the file is gone; remove
        * under a configured root  -> keep (an unticked root stops being
                                      maintained, it is not wiped)
        * under no configured root -> the root was removed; remove
        """

        def normalise(path: str) -> str:
            return os.path.normcase(os.path.normpath(path))

        def under(uri: str, prefixes: list[str]) -> bool:
            if not prefixes:
                return False
            candidate = normalise(uri)
            return any(
                candidate == prefix or candidate.startswith(prefix + os.sep)
                for prefix in prefixes
            )

        seen = {normalise(uri) for uri in seen_uris}
        walked = [normalise(root) for root in walked_roots]
        keep = [
            normalise(root)
            for root in (walked_roots if keep_roots is None else keep_roots)
        ]

        rows = self.con.execute(
            "SELECT id, uri FROM items WHERE parent_id IS NULL AND source = ?", (source,)
        ).fetchall()
        removed = 0
        for row in rows:
            uri = row["uri"]
            if normalise(uri) in seen:
                continue
            if not under(uri, walked) and under(uri, keep):
                continue
            self.drop_children(int(row["id"]))
            self.delete_item(int(row["id"]))
            removed += 1
        self.con.commit()
        return removed

    def delete_by_uri(self, uri: str, source: str = "files") -> int:
        """Remove one top-level item and its descendants. Returns rows removed."""
        row = self.con.execute(
            "SELECT id FROM items WHERE source = ? AND uri = ?", (source, uri)
        ).fetchone()
        if row is None:
            return 0
        item_id = int(row["id"])
        removed = self.drop_children(item_id) + 1
        self.delete_item(item_id)
        self.con.commit()
        return removed

    def delete_under(self, prefix: str) -> int:
        """Remove every top-level item inside *prefix* (a removed root).

        Leaving them would keep a folder you removed fully searchable, which
        reads as a bug even though nothing is broken.
        """
        wanted = os.path.normcase(os.path.normpath(prefix))
        removed = 0
        rows = self.con.execute(
            "SELECT id, uri FROM items WHERE parent_id IS NULL"
        ).fetchall()
        for row in rows:
            candidate = os.path.normcase(os.path.normpath(row["uri"]))
            if candidate == wanted or candidate.startswith(wanted + os.sep):
                removed += self.drop_children(int(row["id"])) + 1
                self.delete_item(int(row["id"]))
        self.con.commit()
        return removed

    def clear(self) -> int:
        """Empty the catalog and the index."""
        total = int(self.con.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        for table in ("ft", "fields", "bodies", "items"):
            self.con.execute(f"DELETE FROM {table}")
        self.con.commit()
        return total

    def sweep_source(self, seen_uris: set[str], source: str) -> int:
        """Delete items of one non-filesystem source that a pass did not see.

        Only call this after a pass that covered the whole scope: sweeping after
        a limited sync would delete every message the pass did not reach.
        """
        wanted = {os.path.normcase(uri) for uri in seen_uris}
        rows = self.con.execute(
            "SELECT id, uri FROM items WHERE parent_id IS NULL AND source = ?",
            (source,),
        ).fetchall()
        removed = 0
        for row in rows:
            if os.path.normcase(row["uri"]) in wanted:
                continue
            self.drop_children(int(row["id"]))
            self.delete_item(int(row["id"]))
            removed += 1
        self.con.commit()
        return removed

    # ------------------------------------------------------------------ verify

    def verify(self, check_paths: bool = True) -> dict:
        """Structural self-check. Every check must report zero.

        This is what makes "it stays correct" testable rather than aspirational:
        the catalog, the stored text, the FTS index and the filesystem are
        cross-checked against each other.
        """
        checks: list[dict] = []

        def add(code: str, description: str, count_sql: str, sample_sql: str) -> None:
            count = int(self.con.execute(count_sql).fetchone()[0] or 0)
            sample = [] if count == 0 else [
                dict(row) for row in self.con.execute(sample_sql).fetchall()
            ]
            checks.append(
                {"code": code, "description": description, "count": count, "sample": sample}
            )

        add(
            "orphan_children",
            "child rows whose parent item is missing",
            "SELECT COUNT(*) FROM items c LEFT JOIN items p ON p.id = c.parent_id "
            "WHERE c.parent_id IS NOT NULL AND p.id IS NULL",
            "SELECT c.id, c.uri FROM items c LEFT JOIN items p ON p.id = c.parent_id "
            "WHERE c.parent_id IS NOT NULL AND p.id IS NULL LIMIT 5",
        )
        add(
            "indexed_without_body",
            "indexed items with no stored text row",
            "SELECT COUNT(*) FROM items i LEFT JOIN bodies b ON b.item_id = i.id "
            "WHERE i.state IN ('indexed','partial') AND b.item_id IS NULL",
            "SELECT i.id, i.uri FROM items i LEFT JOIN bodies b ON b.item_id = i.id "
            "WHERE i.state IN ('indexed','partial') AND b.item_id IS NULL LIMIT 5",
        )
        add(
            "indexed_without_fts",
            "indexed items absent from the search index",
            "SELECT COUNT(*) FROM items i LEFT JOIN ft f ON f.item_id = i.id "
            "WHERE i.state IN ('indexed','partial') AND f.item_id IS NULL",
            "SELECT i.id, i.uri FROM items i LEFT JOIN ft f ON f.item_id = i.id "
            "WHERE i.state IN ('indexed','partial') AND f.item_id IS NULL LIMIT 5",
        )
        add(
            "fts_without_item",
            "search index rows with no catalog item",
            "SELECT COUNT(*) FROM ft f LEFT JOIN items i ON i.id = f.item_id "
            "WHERE i.id IS NULL",
            "SELECT f.item_id FROM ft f LEFT JOIN items i ON i.id = f.item_id "
            "WHERE i.id IS NULL LIMIT 5",
        )
        add(
            "children_of_dead_parent",
            "children whose parent is failed, skipped or deleted",
            "SELECT COUNT(*) FROM items c JOIN items p ON p.id = c.parent_id "
            "WHERE p.state IN ('failed','skipped','deleted')",
            "SELECT c.id, c.uri, p.state AS parent_state FROM items c "
            "JOIN items p ON p.id = c.parent_id "
            "WHERE p.state IN ('failed','skipped','deleted') LIMIT 5",
        )
        add(
            "duplicate_uri",
            "the same uri recorded twice for one source",
            "SELECT COUNT(*) FROM (SELECT source, uri FROM items "
            "GROUP BY source, uri HAVING COUNT(*) > 1)",
            "SELECT source, uri, COUNT(*) AS n FROM items GROUP BY source, uri "
            "HAVING COUNT(*) > 1 LIMIT 5",
        )

        if check_paths:
            missing = [
                {"id": int(row["id"]), "uri": row["uri"]}
                for row in self.con.execute(
                    "SELECT id, uri FROM items "
                    "WHERE parent_id IS NULL AND source = 'files' AND state <> 'deleted'"
                ).fetchall()
                if not os.path.exists(row["uri"])
            ]
            checks.append(
                {
                    "code": "missing_source",
                    "description": "catalogued files that no longer exist on disk",
                    "count": len(missing),
                    "sample": missing[:5],
                }
            )

        problems = sum(check["count"] for check in checks)
        return {"ok": problems == 0, "problems": problems, "checks": checks}

    # ----------------------------------------------------------------- search

    def put_content(
        self,
        item_id: int,
        name: str,
        body: str,
        meta: str,
        stored: str | None = None,
        truncated: bool = False,
    ) -> None:
        self.con.execute("DELETE FROM ft WHERE item_id = ?", (item_id,))
        self.con.execute(
            "INSERT INTO ft (item_id, name, body, meta) VALUES (?, ?, ?, ?)",
            (item_id, name or "", body or "", meta or ""),
        )
        if stored is not None:
            self.con.execute(
                """
                INSERT INTO bodies (item_id, text, truncated) VALUES (?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    text = excluded.text, truncated = excluded.truncated
                """,
                (item_id, stored, 1 if truncated else 0),
            )
        self.con.commit()

    def add_field(self, item_id: int, name: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO fields (item_id, name, value) VALUES (?, ?, ?)",
            (item_id, name, value),
        )

    def search(
        self,
        query: str,
        limit: int = 20,
        markers: tuple[str, str] = ("<<", ">>"),
        scope: str = "all",
    ) -> list[sqlite3.Row]:
        """Run a query. *markers* wrap matched terms in the returned text.

        The default reads well in a terminal. A web caller passes control
        characters it then escapes, so highlighting cannot be confused with
        markup that appears in the indexed content itself.
        """
        start, end = markers
        scope_clause = SCOPES.get(scope, "")
        sql = f"""
        SELECT i.id, i.uri, i.name, i.kind, i.state, i.reason, i.source,
               (SELECT p.uri FROM items p WHERE p.id = i.parent_id) AS parent_uri,
               (SELECT p.name FROM items p WHERE p.id = i.parent_id) AS parent_name,
               (SELECT COUNT(*) FROM items c
                 WHERE c.parent_id = i.id AND NOT ({_IMAGE_TEST}))       AS attachment_count,
               (SELECT COUNT(*) FROM items c
                 WHERE c.parent_id = i.id AND ({_IMAGE_TEST}))           AS inline_count,
               highlight(ft, 1, '{start}', '{end}')                 AS name_hl,
               snippet(ft, 2, '{start}', '{end}', ' … ', 16)        AS body_snip,
               bm25(ft)                                             AS score
        FROM ft
        JOIN items i ON i.id = ft.item_id
        WHERE ft MATCH ?
        {scope_clause}
        ORDER BY score
        LIMIT ?
        """
        return self.con.execute(sql, (query, limit)).fetchall()

    def search_with_fallback(
        self,
        query: str,
        limit: int = 20,
        markers: tuple[str, str] = ("<<", ">>"),
    ) -> tuple[list[sqlite3.Row], str, str | None]:
        """Backwards-compatible view of `search_and_match`."""
        result = self.search_and_match(query, limit=limit, markers=markers)
        return result["rows"], result["strategy"], result["note"]

    def search_and_match(
        self,
        query: str,
        limit: int = 20,
        markers: tuple[str, str] = ("<<", ">>"),
        scope: str = "all",
    ) -> dict:
        """Search as typed, then repair, then broaden.

        Returns the rows plus the *expression that actually matched*. The caller
        needs that expression to highlight a preview with the same semantics:
        re-running the raw text can match nothing while the result list is full.

        Three things go wrong for a normal person typing into a search box, and
        all three look identical - an empty result list:

        1. FTS5 query operators. A colon, quote, parenthesis, `+` or `-` turns
           the input into syntax the parser rejects.
        2. Whole-token matching. `comm` does not match `command`.

        So: run it as written; rebuild from its words if the parser objects;
        then retry words as prefixes.
        """
        query = (query or "").strip()
        if not query:
            return {"rows": [], "strategy": "empty", "note": None, "match": ""}

        tokens = re.findall(r"[0-9A-Za-z_\u00c0-\u024f]+", query)

        # A query whose separators were punctuation reads as one literal string -
        # `$6,450.00`, `a-b`, `x.y`. Loose-matching its tokens finds documents
        # that merely contain the pieces somewhere, which for a price or an
        # identifier is nonsense: `$6,450.00` matched documents holding a 6, a
        # 450 and a 00 that had nothing to do with each other. Match the literal
        # as a phrase, and say plainly when it is not there.
        # `name:report` is valid FTS5 column syntax, not a literal string, so it
        # must keep going down the ordinary path.
        column_scoped = bool(re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*:", query))
        literal = (
            not column_scoped
            and len(tokens) > 1
            and " ".join(tokens) != " ".join(query.split())
        )
        if literal:
            phrase = '"' + " ".join(tokens) + '"'
            try:
                rows = self.search(phrase, limit=limit, markers=markers, scope=scope)
            except sqlite3.OperationalError:
                rows = []
            if rows:
                return {"rows": rows, "strategy": "phrase", "note": None, "match": phrase}
            return {
                "rows": [],
                "strategy": "none",
                "note": "those characters were searched as a single string, and it is not present",
                "match": "",
            }

        try:
            rows = self.search(query, limit=limit, markers=markers, scope=scope)
            if rows:
                return {"rows": rows, "strategy": "exact", "note": None, "match": query}
        except sqlite3.OperationalError:
            pass

        if not tokens:
            return {
                "rows": [], "strategy": "failed",
                "note": "that query contained no searchable words", "match": "",
            }

        rebuilt = " AND ".join(f'"{token}"' for token in tokens)
        try:
            rows = self.search(rebuilt, limit=limit, markers=markers, scope=scope)
        except sqlite3.OperationalError:
            rows = []
        if rows:
            return {"rows": rows, "strategy": "words", "note": None, "match": rebuilt}

        # Broaden to prefixes - but only words. Starring a number turns a
        # specific search into nonsense: `$6,450.00` would match any document
        # holding a number that starts with 6, one that starts with 450 and one
        # that starts with 00, and report hits containing none of it.
        if not any(any(character.isalpha() for character in token) for token in tokens):
            return {"rows": [], "strategy": "none", "note": None, "match": ""}

        widened = " AND ".join(
            f'"{token}"*' if any(c.isalpha() for c in token) else f'"{token}"'
            for token in tokens
        )
        try:
            rows = self.search(widened, limit=limit, markers=markers, scope=scope)
        except sqlite3.OperationalError:
            return {
                "rows": [], "strategy": "failed",
                "note": "that query could not be interpreted", "match": "",
            }
        if rows:
            return {
                "rows": rows,
                "strategy": "prefix",
                "note": "no whole-word matches, so this is showing words that start with your terms",
                "match": widened,
            }
        return {"rows": [], "strategy": "none", "note": None, "match": ""}

    def scope_counts(self) -> dict:
        """How many items each navigation scope holds."""
        conditions = {
            "all": "",
            "files": "WHERE source = 'files'",
            "email": "WHERE kind = 'message'",
            "attachments": "WHERE kind IN ('attachment', 'archive-member')",
        }
        return {
            name: int(self.con.execute(f"SELECT COUNT(*) FROM items {where}").fetchone()[0])
            for name, where in conditions.items()
        }

    def get_body(self, item_id: int, chars: int = 4000) -> str:
        row = self.con.execute(
            "SELECT text FROM bodies WHERE item_id = ?", (item_id,)
        ).fetchone()
        if not row or not row["text"]:
            return ""
        return row["text"][:chars]

    def highlighted_body(
        self,
        item_id: int,
        query: str,
        markers: tuple[str, str] = ("[", "]"),
    ) -> str:
        """The indexed text with every match of *query* marked.

        Uses the same engine that matched the row, so the marks land exactly
        where the search found the term - including occurrences past the point
        where the stored preview was truncated.
        """
        start, end = markers
        row = self.con.execute(
            f"SELECT highlight(ft, 2, '{start}', '{end}') AS body "
            "FROM ft WHERE ft MATCH ? AND ft.item_id = ?",
            (query, item_id),
        ).fetchone()
        return row["body"] if row and row["body"] else ""

    def get_fields(self, item_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT name, value FROM fields WHERE item_id = ? ORDER BY name", (item_id,)
        ).fetchall()

    def children(self, item_id: int) -> list[sqlite3.Row]:
        """Attachments or archive members belonging to *item_id*.

        `has_text` is what tells a caller whether the child's *contents* were
        searchable, as opposed to its name only.
        """
        return self.con.execute(
            """
            SELECT id, name, kind, state, reason,
                   EXISTS (SELECT 1 FROM bodies b
                            WHERE b.item_id = items.id AND b.text <> '') AS has_text
            FROM items
            WHERE parent_id = ?
            ORDER BY name
            """,
            (item_id,),
        ).fetchall()

    def get_item_by_id(self, item_id: int):
        return self.con.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    # ------------------------------------------------------------------ stats

    def stats(self) -> dict:
        def scalar(sql: str, *args) -> int:
            row = self.con.execute(sql, args).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        return {
            "items_total": scalar("SELECT COUNT(*) FROM items"),
            "items_top_level": scalar("SELECT COUNT(*) FROM items WHERE parent_id IS NULL"),
            "items_attachments": scalar("SELECT COUNT(*) FROM items WHERE kind = 'attachment'"),
            "items_archive_members": scalar("SELECT COUNT(*) FROM items WHERE kind = 'archive-member'"),
            "indexed": scalar("SELECT COUNT(*) FROM items WHERE state = 'indexed'"),
            "partial": scalar("SELECT COUNT(*) FROM items WHERE state = 'partial'"),
            "failed": scalar("SELECT COUNT(*) FROM items WHERE state = 'failed'"),
            "skipped": scalar("SELECT COUNT(*) FROM items WHERE state = 'skipped'"),
            "with_text": scalar("SELECT COUNT(*) FROM bodies WHERE text IS NOT NULL AND text <> ''"),
            "stored_text_chars": scalar("SELECT COALESCE(SUM(LENGTH(text)), 0) FROM bodies"),
            "fields": scalar("SELECT COUNT(*) FROM fields"),
            "index_rows": scalar("SELECT COUNT(*) FROM ft"),
            "db_bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0,
        }

    def top_terms(self, limit: int = 20) -> list[tuple[str, int, int]]:
        try:
            self.con.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS ft_vocab USING fts5vocab(ft, 'row')"
            )
        except sqlite3.OperationalError:
            return []
        rows = self.con.execute(
            "SELECT term, doc, cnt FROM ft_vocab ORDER BY cnt DESC, term LIMIT ?", (limit,)
        ).fetchall()
        return [(r["term"], int(r["doc"]), int(r["cnt"])) for r in rows]
