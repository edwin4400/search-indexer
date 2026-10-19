# Content search pipeline - first slice

The command-line implementation of `../../content-search/docs/architecture-sketch.md`.
It covers the foundation and correctness groups of
[checklist.md](../../content-search/docs/checklist.md), and evidence for each
checked item there lives in this directory.

What it does: crawl a tree, extract text from files, mail and their attachments,
index it, query it - then keep it correct as files change, by watching for
edits, skipping rewrites whose text did not change, recognising moves,
cascading deletes, and self-checking that the catalog, stored text, index and
filesystem agree.

The directory is named `m0/` from before the plan was recut as a checklist. The
name is historical; the contents are current.

## Run it

```powershell
python tools/make_sample.py                 # build the demo corpus
python search.py index sample -v            # crawl -> extract -> index
python search.py query escrow               # search
python search.py stats                      # catalog totals
python search.py terms --limit 10           # most frequent terms
python search.py show 1 --chars 500         # one item's fields and stored text
python search.py watch sample --interval 2  # incremental updates (Ctrl+C)
python search.py verify                     # correctness self-check
```

`--db` selects a different index. Query syntax is FTS5: bare terms AND together,
`"quoted phrases"` are exact, `name:term` / `body:term` / `meta:term` restrict to
a column.

`index` and `watch` accept several roots. `verify` exits non-zero if the
catalog, stored text, search index and filesystem disagree.

## The app

```powershell
python run.py                 # starts the local service and opens a browser
python run.py --no-browser    # for a headless or remote session
python run.py --port 8801
```

Search box, results list with highlighted matches, preview of stored text, and a
Folders panel to add roots, index on demand, watch continuously, and run
`verify`. Roots and watch settings persist in `app/config.json`.

**Transport choice.** The sketch assumed a .NET shell. This is a local HTTP
service plus a browser front end instead - no new toolchain, no build step, and
it reuses the pipeline directly. A native shell could replace it without
touching `service.py`.

Three constraints, because it is a service that reads your files:

1. **Loopback only** - binds `127.0.0.1`, so nothing on the network reaches it.
2. **Token on every API call** - passed in the launch URL, kept in
   `sessionStorage`, sent as `X-Token`. A custom header cannot be set by a
   cross-site form post, so a hostile page cannot drive the API. A present
   `Origin` must also be loopback.
3. **A fixed static whitelist** - four files are served, so there is no
   path-traversal surface.

Indexed content is untrusted, so it never reaches the browser as markup: the
server escapes it with `html.escape` and only then substitutes match markers.

### Verified over the API

| Check | Result |
| --- | --- |
| UI and assets served | `/`, `/app.js`, `/style.css` all 200 |
| Unauthenticated request | 401 |
| Bad token | 401 |
| Foreign `Origin` | 401 |
| Add root, index | 10 items, 1 attachment |
| Content search | `escrow` -> attachment, parent `vendor-quote.eml` |
| Highlighting | matched terms returned inside `<mark>` |
| Fielded search | `name:quarterly` -> 1 |
| Preview | fields and stored text returned |
| `verify` | ok, 0 problems |
| Live watch: create / modify / delete | indexed, re-indexed, removed |
| Untrusted content | `<script>` returned escaped; a literal `<<b>>` rendered as text, not markup |
| Folder added while watching | adopted and indexed on the next scan, other roots untouched |
| Folder removed | its items purged; other roots untouched |
| Junk names | `~$*`, `.DS_Store`, `Thumbs.db`, `*.tmp` skipped |
| Unknown extension holding text | `.out` file indexed by content: 15,741 chars, `raspi` / `bluetooth` / `systemctl` all match |

Not done here: **packaging** (no installer), and the **extraction sandbox** - so
the UI states plainly that it is for folders you trust.

## Layout

```
m0/
  run.py                        launcher: starts the app, opens the browser
  app/
    service.py                  roots, indexing, watching, queries (no HTTP)
    server.py                   loopback HTTP, token auth, static whitelist
    static/                     index.html, app.js, style.css - no build step
    config.json                 generated: saved roots and watch settings
  search.py                     CLI: index | query | stats | terms | show
  pipeline/
    crawl.py                    stage 1  enumerate + hash files
    extract/                    stage 2  format handlers, one interface
      __init__.py               registry, Limits, SubItem, ExtractedText
      text.py                   txt/md/code/csv/html
      ooxml.py                  docx/xlsx/pptx via stdlib zip+ElementTree
      eml.py                    RFC822 email -> headers, body, attachments
      archive.py                zip -> members
      pdf.py                    PyPDF2 when present, loud when not
    analyze.py                  stage 3  folding, normalization, stop words
    index.py                    stage 4/5  catalog + FTS5 index + search
    watch.py                    stage 1b  polling change detection
    __init__.py                 orchestration, attachment recursion, incremental update
  tools/make_sample.py          generates the demo corpus
  tools/selftest.py             36-check correctness suite
  sample/                       generated; safe to delete
  index/                        generated .db files; safe to delete
  testrun/                      generated by the self-test; safe to delete
```

## Verified behaviour

Each fixture carries one distinctive token, so a hit proves which stage surfaced
it. All queries returned in 0.2-0.4 ms.

| Query | Surfaces | Proves |
| --- | --- | --- |
| `kazoo` | `meeting-notes.txt` | plain-text body |
| `parsnip` | `README-notes.md` | markup |
| `amortization` | `quarterly-report.docx` | OOXML in a container |
| `consultancy` | `budget.xlsx` | spreadsheet, shared-string resolution |
| `indemnity` | `vendor-terms.pdf` | PDF text layer |
| `vendor` | `vendor-quote.eml`, `vendor-terms.pdf` | **filename** field |
| `netterms` | `vendor-quote.eml` body | email body |
| `escrow` | `pricing-sheet.txt`, **in: vendor-quote.eml** | **attachment content + parent resolution** |
| `rhubarb` | `brief.txt`, **in: deliverables.zip** | **archive member content + parent resolution** |
| `"escrow deposit"` | attachment | phrase search (needs positions) |
| `name:vendor` | 2 files | fielded column query |

Two runs:

| Corpus | Files | Items after recursion | Index time | DB |
| --- | --- | --- | --- | --- |
| `sample/` | 7 | 10 (1 attachment, 2 archive members) | 0.12 s | 52 KB |
| `../` (this analysis folder) | 37 | 40 | 0.87 s | 3.7 MB |

## Incremental correctness

Verified by `python tools/selftest.py`, which builds its own corpus and
asserts 36 behaviours. Current result: **36/36 pass**. An unchanged tree is
indexed in 0.00 s because no file is read.

How it works - change detection is a **two-tier gate**, cheapest first:

| Tier | Test | Cost | Outcome |
| --- | --- | --- | --- |
| 1 | `(size, mtime)` identical to the catalog | `stat` only, no read | skip the file entirely |
| 2 | extracted-text fingerprint identical | one parse | refresh bookkeeping, leave the index untouched |
| 3 | otherwise | full re-extract | update catalog, stored text and index |

Tier 2 is what fixes the container-rewrite problem noted below. A `.docx`
rewritten by a sync client gets
new bytes (zip timestamps, MIME boundaries) but identical text; it is recognised
and the FTS rows are not rewritten at all. The self-test proves that by capturing
the FTS `rowid` before and after - it must not change.

Other incremental behaviour, each with a passing check:

| Behaviour | Check |
| --- | --- |
| Edit re-extracts and drops stale terms | C1-C3 |
| Same-text rewrite leaves the index alone | D1-D5 |
| Deleting an email removes its attachment | E1-E4 |
| Rename keeps item identity, no duplicate | F1-F5 |
| Unreadable file is retryable, not lost | G1-G3 |
| Watcher applies create/modify/delete live | H1-H6 |

### Design notes

**Moves keep identity.** A file that reappears at a new path with the same
extracted text is re-pointed rather than deleted and reinserted, so its item id,
stored text and children survive. The self-test asserts the id is unchanged.

**Retryable reads.** A file being written looks like a read failure. That is
reported as retryable, and the watcher keeps the *previous* signature for that
path so the next pass sees the change again instead of silently losing it.

**Timestamps carry sub-second precision.** Change detection compares mtimes, so
second-granularity timestamps would miss two saves inside one second that leave
the size unchanged. `mtime` is stored with microseconds.

**The connection is shared across threads deliberately.** The watcher writes from
its own thread while callers read from another, so the connection is opened with
`check_same_thread=False`; SQLite serialises access internally and WAL mode keeps
readers from blocking the writer. Without it the watcher dies the moment it
applies a change - which is exactly what the first self-test run caught.

**Polling, not OS notifications.** Change detection diffs a `(size, mtime)`
snapshot of each root, so it depends on no platform notification semantics and
is portable. The cost is latency: changes are seen on the next pass, not
instantly. The source is isolated behind `scan()` so a `ReadDirectoryChangesW`
implementation can replace it without touching anything else.

## Design decisions carried over from the sketch

- **One index, name and body as sibling columns.** Filename search is not a
  separate mechanism - `name:vendor` and a body hit are the same query path.
- **Attachments are sub-items.** `SubItem` is returned by the extractor and the
  pipeline creates a child row with `parent_id`, so a hit inside an attachment
  resolves to its parent email. Archive members work identically.
- **Extraction behind one interface.** `extract_bytes(name, data, limits)` never
  raises; a failure becomes a `reason` on the item's state, so one bad file
  cannot stall a crawl.
- **Limits are enforced centrally** (`Limits`): max document bytes, max extracted
  characters, max sub-items, recursion depth, max archive member size.
- **Stored text is separate from indexed text** so the preview path never
  re-opens the original file.

## Divergences from the sketch, and why

**Python, not .NET 8.** The machine has only .NET 3.1 and 5.0 SDKs (both EOL).
Python 3.12 plus SQLite FTS5 is already present and needs zero new dependencies,
and FTS5 supplies positions, phrase search, fielded columns, BM25 and
`highlight()`/`snippet()`. The architecture, interfaces and schema are
unchanged; the findings here port to .NET directly if that is still the target.

**Stop words are applied at query time, not index time.** X1 strips stop words
while indexing (its shipped `english.stop`), which is compact but makes phrase
search lossy. This build keeps the index lossless so `"exact phrase"` is exact.

## Findings from actually running it

1. **Container re-indexing was wasteful - since fixed.** The first version
   re-extracted every `.docx`, `.xlsx`, `.zip` and `.eml` on every pass, because
   ZIP parts store modification times and MIME boundaries are randomised, so the
   *file* hash changed even when the *content* did not. The text fingerprint
   (tier 2 above) fixes it, and the self-test covers it as case D.
2. **Binaries degrade loudly, not silently.** Files with no registered extractor
   (the Ghidra snapshot, SQLite databases) are recorded as `partial` with an
   explicit reason rather than dropped.
3. **PDF works, OCR does not.** PyPDF2 handles a text-layer PDF; a scanned PDF is
   reported as `no text layer - needs OCR` rather than indexing as empty.
4. **Non-ASCII survives.** Verified directly against the database: `U+2014` is
   present and the stored prefix matches the source byte-for-byte. The `?` seen
   in console output is terminal rendering, not data loss.
5. **Two bugs the self-test caught.** The first run failed 4 of 36 checks: the
   watcher raised "SQLite objects created in a thread can only be used in that
   same thread" on its first write, and edits of known files were classified as
   `indexed` instead of `updated`. Both are fixed, and the checks now pass; the
   suite is worth re-running after any pipeline change.
6. **The deletion sweep was per root - a data-loss bug.** Indexing root A
   deleted every item belonging to root B, because B's files were never walked
   during that pass. It only appears with two or more folders configured, which
   is why every test up to that point passed: they all used one root. The sweep
   now takes the walked roots and the configured roots together, so a file that
   vanished inside a walked root is removed, an unticked root is left alone, and
   a removed root is purged. Self-test case J covers it.
7. **A running watcher ignored folders added afterwards.** The watcher
   snapshotted its root list at construction, so a folder added while watching
   was never scanned - and because the watcher held the worker lock, "Index now"
   and "Watch" were both refused, leaving no way forward except Stop then Watch.
   The root list is now re-read every pass and a new root is indexed on
   adoption. Adding a folder also starts indexing it directly when idle.
8. **"No results" covered three different situations.** FTS5 matches whole
   tokens, so typing part of a word found nothing; and a query containing `:`,
   `"`, `(`, `+` or `-` was rejected by the parser and then retried as one exact
   phrase, which almost never matches. Both looked identical to an empty index.
   Queries are now run as typed, rebuilt from their words if the parser objects,
   then retried as prefixes if nothing matched - and the UI says when it
   broadened. Verified: `comm` went from 0 to 14 hits, `command: linux` from 0 to
   3, `out.txt` from 0 to 2. Self-test case K covers it.
9. **An unregistered extension silently meant "no text".** A file called
   `linux command bluetooth raspi.out` matched by filename, but searching its
   contents found nothing: `.out` had no extractor, so the file was catalogued
   without ever being read. A file whose suffix is not recognised is now sniffed
   as text - a NUL byte or a high proportion of control characters means binary
   - so `.out`, `.log1` and extensionless text files are indexed normally, and
   genuinely binary files record why they were skipped. Common command-output
   suffixes were added to the explicit list too.
10. **A file that failed to extract was never retried.** The change gate skipped
    anything already catalogued, so a file marked `partial` stayed `partial`
    forever - even after an extractor that could read it existed. A full pass now
    re-attempts `partial` items, and the "text unchanged" fast path corrects
    `state` instead of only refreshing bookkeeping. Self-test L5-L6.
11. **A live Outlook is a dead end, so the store is read offline.** A running
    Outlook byte-range locks its `.ost`, so the file cannot be read or even
    copied, and COM automation from a non-interactive process fails with
    `ERROR_NO_SUCH_LOGON_SESSION`. The connector therefore parses the PST/OST
    directly with libpff (`pypff`): no Outlook, no interactive session, no lock.
    Verified against a real store - `marketing` returns the matching message,
    kind `message`, uri `outlook-store://<store>/<folder>/<id>` - and
    attachments ride the existing sub-item path. `--via-com` remains for a store
    that has to stay open.
12. **A mis-detected encoding killed the whole mail sync.** Python's `utf-16`
    codec accepts *unpaired surrogates* and returns them unchanged, so a body
    that was really cp1252 but tried as utf-16 produced a string SQLite cannot
    encode (`UnicodeEncodeError: surrogates not allowed`) and the insert failed.
    Fixed in three places: utf-16 is now only attempted when the bytes look like
    utf-16 (a BOM, or alternating NULs); every extractor's output is scrubbed of
    surrogates before it reaches the index; and a single unreadable message is
    recorded as `failed` instead of abandoning the mailbox. Self-test N1-N8.
13. **The snippet and the preview disagreed because only one of them knew about
    the query.** `snippet()` quotes one occurrence out of many, and the preview
    had no highlighting at all, so a document with eight occurrences of a term
    looked like it contained a different sentence. The preview is now rendered
    from the index itself via `highlight()`, so every occurrence is marked and
    the count is reported - and because that comes from the indexed text rather
    than the stored copy, matches past the 50k preview cap are still found, with
    the window centred on the first one. Self-test P1-P5.
14. **A punctuated query was loose-matched into nonsense, and the preview
    re-ran the raw text so nothing highlighted.** `$6,450.00` was tokenised to
    `6 AND 450 AND 00`, which matched four unrelated PDFs that happened to
    contain those numbers separately. Meanwhile the preview highlighted using
    the text the user typed, which matched nothing, so it marked nothing.
    Two changes: the preview now highlights with the *expression that matched*,
    carried from the search to the item request; and a query whose separators
    were punctuation is treated as one literal string, so if it is absent the
    answer is no results rather than a pile of coincidences. Column syntax
    (`name:`, `meta:`) is explicitly excluded from the literal rule - the suite
    caught that regression immediately. Self-test R1-R5, K3, A7.
15. **The match counter offered fewer matches than the document had.** The
    preview windowed documents to 40k characters, so a PDF with 36 matches
    exposed only the 4 inside that window - the toolbar said "3 of 4 - 36 in the
    document" and there was no way to reach the rest. Documents are now sent
    whole up to 1,000,000 characters, so every match is steppable; the largest
    body in this mailbox is 721k, and a 544k document round-trips in 46 ms.
    Only oversized documents are still windowed, and only then does the caveat
    appear. Self-test S1-S5.
16. **Two traps in packaging the executable.** `--add-data` resolves its source
    relative to the spec folder rather than the working directory, so the static
    UI was not found until the path was made absolute. And the ambient
    `PYTHONPATH` in this environment pointed at unrelated projects, which
    PyInstaller duly collected - clearing it and excluding pygame/tkinter/matplotlib
    cut the bundle to 34 MB. `pypff`, `PyPDF2` and `win32com` are imported inside
    functions so the app runs without them, which hides them from static analysis:
    they must be declared as hidden imports or the packaged mail/PDF paths fail.
17. **The panes kept stale content, and the interface looked like unstyled
    markup.** Clearing the search box left the previous result list and document
    on screen, and starting a new search left an item the new results no longer
    contained. Both reset now. The interface was rebuilt on a small design system
    - one accent, three surfaces, a fixed type scale - with a real shell, a rail
    of scopes carrying counts, result cards with kind chips, a segmented
    prev/next match control, and empty states. Two flaws only showed up when the
    page was rendered headlessly: the preview scrolled past its own title and
    toolbar on open, and Chromium drew a stray dropdown arrow for an empty recent
    list. Deep links (`?q=...&open=...`) came out of the work, and are how those
    renders are produced.

## Still missing

No IMAP or other mail connector, no service/IPC split, no out-of-process
extraction sandbox (the interface is in place; the process boundary is not), no
OCR, no `.msg` / legacy `.doc` / `.xls` handling, no cloud connectors, no ranking
tuning, no UI. Extraction is still in-process, so it is not yet safe against
hostile input - that belongs with the sandbox work.
