#!/usr/bin/env python3
"""Content search CLI - crawl, extract, index, query, watch, verify.

    python search.py index <root> [<root> ...] [--db PATH] [-v]
    python search.py query "<terms>" [--db PATH] [--limit N] [--json]
    python search.py stats [--db PATH]
    python search.py terms [--db PATH] [--limit N]
    python search.py show <item_id> [--db PATH] [--chars N]
    python search.py watch <root> [<root> ...] [--interval S] [--iterations N]
    python search.py verify [--db PATH]

Query syntax is FTS5: bare terms are ANDed, "quoted phrases" are exact, and
`name:term`, `body:term`, `meta:term` restrict to one column.

`verify` is the correctness gate: it cross-checks the catalog, the stored text,
the FTS index and the filesystem against each other and fails if they disagree.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import Builder                                    # noqa: E402
from pipeline.analyze import load_stop_words, query_is_only_stop_words  # noqa: E402
from pipeline.extract import Limits                            # noqa: E402
from pipeline.index import Index                               # noqa: E402
from pipeline.watch import Watcher                             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "index", "search.db")


def _human(bytes_count: int) -> str:
    value = float(bytes_count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def cmd_index(args: argparse.Namespace) -> int:
    roots = [os.path.abspath(r) for r in args.root]
    for root in roots:
        if not os.path.isdir(root):
            print(f"error: not a directory: {root}", file=sys.stderr)
            return 2

    limits = Limits(
        max_doc_bytes=args.max_doc_bytes,
        max_subitems=args.max_subitems,
        max_depth=args.max_depth,
    )
    index = Index(args.db)
    builder = Builder(index, limits=limits, verbose=args.verbose)

    started = time.perf_counter()
    stats: Counter[str] = Counter()
    stats.update(builder.build_roots(roots))
    elapsed = time.perf_counter() - started
    totals = index.stats()
    index.close()

    print(f"indexed {len(roots)} root(s) in {elapsed:.2f}s")
    for root in roots:
        print(f"  {root}")
    for key in ("indexed", "updated", "moved", "text_unchanged", "unchanged",
                "attachment", "archive-member", "skipped", "failed", "deleted"):
        if stats.get(key):
            print(f"  {key:<14} {stats[key]}")
    print(f"  catalog        {totals['items_total']} items "
          f"({totals['items_top_level']} files, {totals['items_attachments']} attachments, "
          f"{totals['items_archive_members']} archive members)")
    print(f"  stored         {totals['stored_text_chars']:,} chars across {totals['with_text']} items")
    print(f"  db             {_human(totals['db_bytes'])}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    index = Index(args.db)
    stop_words = load_stop_words()
    if query_is_only_stop_words(args.query, stop_words):
        print("warning: query consists only of stop words", file=sys.stderr)

    started = time.perf_counter()
    rows, strategy, note = index.search_with_fallback(args.query, limit=args.limit)
    elapsed_ms = (time.perf_counter() - started) * 1000

    if args.json:
        print(json.dumps(
            {
                "query": args.query,
                "elapsed_ms": round(elapsed_ms, 2),
                "count": len(rows),
                "strategy": strategy,
                "note": note,
                "results": [dict(row) for row in rows],
            },
            indent=2,
        ))
        index.close()
        return 0

    print(f'{len(rows)} result(s) for "{args.query}" in {elapsed_ms:.1f} ms')
    if note:
        print(f"  note: {note}")
    for number, row in enumerate(rows, start=1):
        label = row["name_hl"] or row["name"]
        print(f"\n{number}. {label}   [{row['kind']}, {row['state']}]  score={row['score']:.2f}")
        if row["parent_name"]:
            print(f"   in: {row['parent_name']}")
        print(f"   {row['uri']}")
        if row["body_snip"]:
            print(f"   {row['body_snip']}")
        if row["reason"]:
            print(f"   note: {row['reason']}")
    index.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    index = Index(args.db)
    totals = index.stats()
    index.close()
    print(json.dumps(totals, indent=2))
    return 0


def cmd_outlook(args: argparse.Namespace) -> int:
    """Sync Outlook mail and attachments.

    Offline by default: the PST/OST file is parsed directly, which needs no
    Outlook and is not affected by the logon-session problem that stops COM
    automation from a non-interactive process. Pass --via-com to read a running
    Outlook instead.
    """
    from pipeline.mail_outlook import OutlookUnavailable
    from pipeline.mail_pst import StoreUnavailable, discover_stores

    index = Index(args.db)
    builder = Builder(index, limits=Limits(), verbose=args.verbose)

    if args.list_stores:
        stores = discover_stores()
        if not stores:
            print("no .pst or .ost found in the usual locations")
        for path in stores:
            print(f"  {os.path.getsize(path) / 1048576:9.1f} MB  {path}")
        index.close()
        return 0

    try:
        if args.via_com:
            import pythoncom

            from pipeline.mail_outlook import iter_messages as iter_com

            pythoncom.CoInitialize()
            try:
                records = iter_com(
                    store=args.store, folder=args.folder, limit=args.limit,
                    include_attachments=not args.no_attachments,
                )
                started = time.perf_counter()
                stats = builder.build_mail(records, sweep=args.limit is None)
                elapsed = time.perf_counter() - started
            finally:
                pythoncom.CoUninitialize()
        else:
            from pipeline.mail_pst import iter_messages as iter_store

            store_file = args.store_file
            if not store_file:
                candidates = discover_stores()
                if not candidates:
                    print("error: no .pst or .ost found; pass --store-file", file=sys.stderr)
                    return 2
                store_file = candidates[0]
            print(f"store: {store_file}")
            records = iter_store(
                store_file, folder=args.folder, limit=args.limit,
                include_attachments=not args.no_attachments,
            )
            started = time.perf_counter()
            stats = builder.build_mail(records, sweep=args.limit is None)
            elapsed = time.perf_counter() - started
    except (OutlookUnavailable, StoreUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    totals = index.stats()
    index.close()
    print(f"synced Outlook in {elapsed:.1f}s")
    for key in ("indexed", "updated", "unchanged", "attachment", "partial", "deleted"):
        if stats.get(key):
            print(f"  {key:<12} {stats[key]}")
    print(f"  catalog      {totals['items_total']} items, {totals['with_text']} with text")
    if args.limit is not None:
        print("  note: a limited sync does not remove messages deleted in Outlook")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    index = Index(args.db)
    report = index.verify(check_paths=not args.no_paths)
    index.close()

    print(f"{'':<3} {'check':<24} {'count':>7}  description")
    for check in report["checks"]:
        flag = "ok " if check["count"] == 0 else "BAD"
        print(f"{flag} {check['code']:<24} {check['count']:>7}  {check['description']}")
        for row in check["sample"]:
            print(f"      {row}")
    print()
    if report["ok"]:
        print("PASS - catalog, stored text, index and filesystem agree")
    else:
        print(f"FAIL - {report['problems']} problem(s)")
    return 0 if report["ok"] else 1


def cmd_watch(args: argparse.Namespace) -> int:
    roots = [os.path.abspath(r) for r in args.root]
    for root in roots:
        if not os.path.isdir(root):
            print(f"error: not a directory: {root}", file=sys.stderr)
            return 2

    index = Index(args.db)
    builder = Builder(
        index,
        limits=Limits(
            max_doc_bytes=args.max_doc_bytes,
            max_subitems=args.max_subitems,
            max_depth=args.max_depth,
        ),
        verbose=args.verbose,
    )

    if args.reconcile:
        started = time.perf_counter()
        for root in roots:
            builder.build_root(root)
        print(f"reconciled {len(roots)} root(s) in {time.perf_counter() - started:.2f}s")

    watcher = Watcher(builder, roots, interval=args.interval, verbose=args.verbose)
    horizon = f"{args.iterations} pass(es)" if args.iterations else "Ctrl+C to stop"
    print(f"watching {len(roots)} root(s), interval {args.interval}s, {horizon}")

    try:
        watcher.run(iterations=args.iterations)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        index.close()

    if watcher.changes:
        print(f"{len(watcher.changes)} change(s) applied:")
        for change, outcome in watcher.changes:
            print(f"  {change.kind:<8} {change.path}  -> {outcome}")
    else:
        print("no changes")
    return 0


def cmd_terms(args: argparse.Namespace) -> int:
    index = Index(args.db)
    rows = index.top_terms(args.limit)
    index.close()
    if not rows:
        print("no vocabulary available")
        return 0
    print(f"{'term':<28} {'items':>7} {'occurrences':>12}")
    for term, documents, occurrences in rows:
        print(f"{term:<28} {documents:>7} {occurrences:>12}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    index = Index(args.db)
    item = index.get_item_by_id(args.item_id)
    if item is None:
        print(f"no such item: {args.item_id}", file=sys.stderr)
        index.close()
        return 2
    print(f"name  : {item['name']}")
    print(f"uri   : {item['uri']}")
    print(f"kind  : {item['kind']}   state: {item['state']}")
    if item["reason"]:
        print(f"reason: {item['reason']}")
    fields = index.get_fields(args.item_id)
    if fields:
        print("fields:")
        for field in fields:
            print(f"  {field['name']}: {field['value']}")
    body = index.get_body(args.item_id, chars=args.chars)
    print("stored text:")
    print(body or "  (none)")
    index.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="search.py", description="content search pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="crawl a folder, extract, and index it")
    p_index.add_argument("root", nargs="+")
    p_index.add_argument("--db", default=DEFAULT_DB)
    p_index.add_argument("-v", "--verbose", action="store_true")
    p_index.add_argument("--max-doc-bytes", type=int, default=Limits.max_doc_bytes)
    p_index.add_argument("--max-subitems", type=int, default=Limits.max_subitems)
    p_index.add_argument("--max-depth", type=int, default=Limits.max_depth)
    p_index.set_defaults(func=cmd_index)

    p_query = sub.add_parser("query", help="search the index")
    p_query.add_argument("query")
    p_query.add_argument("--db", default=DEFAULT_DB)
    p_query.add_argument("--limit", type=int, default=20)
    p_query.add_argument("--json", action="store_true")
    p_query.set_defaults(func=cmd_query)

    p_stats = sub.add_parser("stats", help="catalog and index totals")
    p_stats.add_argument("--db", default=DEFAULT_DB)
    p_stats.set_defaults(func=cmd_stats)

    p_terms = sub.add_parser("terms", help="most frequent indexed terms")
    p_terms.add_argument("--db", default=DEFAULT_DB)
    p_terms.add_argument("--limit", type=int, default=20)
    p_terms.set_defaults(func=cmd_terms)

    p_show = sub.add_parser("show", help="show one item's fields and stored text")
    p_show.add_argument("item_id", type=int)
    p_show.add_argument("--db", default=DEFAULT_DB)
    p_show.add_argument("--chars", type=int, default=2000)
    p_show.set_defaults(func=cmd_show)

    p_watch = sub.add_parser("watch", help="watch roots and apply changes incrementally")
    p_watch.add_argument("root", nargs="+")
    p_watch.add_argument("--db", default=DEFAULT_DB)
    p_watch.add_argument("--interval", type=float, default=2.0,
                         help="seconds between change scans")
    p_watch.add_argument("--iterations", type=int, default=None,
                         help="stop after N scans (used by tests)")
    p_watch.add_argument("--reconcile", action="store_true", default=True,
                         help="full reconcile before watching (default)")
    p_watch.add_argument("--no-reconcile", dest="reconcile", action="store_false")
    p_watch.add_argument("-v", "--verbose", action="store_true")
    p_watch.add_argument("--max-doc-bytes", type=int, default=Limits.max_doc_bytes)
    p_watch.add_argument("--max-subitems", type=int, default=Limits.max_subitems)
    p_watch.add_argument("--max-depth", type=int, default=Limits.max_depth)
    p_watch.set_defaults(func=cmd_watch)

    p_outlook = sub.add_parser(
        "outlook", help="sync Outlook mail and attachments from a PST/OST file")
    p_outlook.add_argument("--db", default=DEFAULT_DB)
    p_outlook.add_argument("--store-file", default=None,
                           help="PST/OST to read; default is the largest one found")
    p_outlook.add_argument("--store", default=None, help="store name; default is every store")
    p_outlook.add_argument("--folder", default=None, help="only this folder name")
    p_outlook.add_argument("--limit", type=int, default=None,
                           help="stop after N messages; disables deletion sweep")
    p_outlook.add_argument("--no-attachments", action="store_true")
    p_outlook.add_argument("--list-stores", action="store_true")
    p_outlook.add_argument("--via-com", action="store_true",
                           help="read a running Outlook instead of the file")
    p_outlook.add_argument("-v", "--verbose", action="store_true")
    p_outlook.set_defaults(func=cmd_outlook)

    p_verify = sub.add_parser(
        "verify", help="cross-check catalog, stored text, index and filesystem")
    p_verify.add_argument("--db", default=DEFAULT_DB)
    p_verify.add_argument("--no-paths", action="store_true",
                          help="skip the filesystem existence check")
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
