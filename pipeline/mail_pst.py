"""Outlook store connector for PST/OST files, offline, via libpff (pypff).

Preferred over COM automation for two reasons that both bit us:

* A store Outlook has open is byte-range locked, so COM is the only way to read
  it while Outlook runs - and COM fails with ERROR_NO_SUCH_LOGON_SESSION when
  the caller is not in the same interactive logon session as Outlook.
* Offline parsing needs no Outlook at all, and works on any machine holding the
  file.

So: close Outlook, then sync. The error message says exactly that rather than
surfacing a libpff stack trace.

Messages become items and attachments become sub-items, so extraction, parent
resolution and cascade delete are the same code as for files and .eml.
"""

from __future__ import annotations

import datetime
import os

from .analyze import normalize, scrub, strip_markup

STORE_SUFFIXES = (".pst", ".ost")
MAX_FOLDER_DEPTH = 12
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Where Outlook keeps stores, most specific first.
DISCOVERY_DIRS = (
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Outlook"),
    os.path.join(os.path.expanduser("~"), "Documents", "Outlook Files"),
    os.path.expanduser("~"),
)


class StoreUnavailable(RuntimeError):
    """The store could not be opened."""


def discover_stores() -> list[str]:
    """Every PST/OST in the usual locations, largest first."""
    found: dict[str, int] = {}
    for directory in DISCOVERY_DIRS:
        if not directory or not os.path.isdir(directory):
            continue
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                if not entry.name.lower().endswith(STORE_SUFFIXES):
                    continue
                try:
                    found[entry.path] = entry.stat().st_size
                except OSError:
                    continue
    return [path for path, _ in sorted(found.items(), key=lambda kv: -kv[1])]


def _open(path: str):
    import pypff

    if not os.path.exists(path):
        raise StoreUnavailable(f"no such store: {path}")
    store = pypff.file()
    try:
        store.open(path)
    except OSError as exc:
        detail = str(exc)
        if "locked" in detail or "in use" in detail.lower():
            # The common case, and the only one with a one-step fix.
            raise StoreUnavailable(
                f"{os.path.basename(path)} is locked by another process.\n"
                "  Outlook holds its data file open while running. Close Outlook "
                "completely, then run the sync again."
            ) from exc
        raise StoreUnavailable(
            f"cannot read {os.path.basename(path)}: {detail.splitlines()[0][:160]}"
        ) from exc
    return store


def _stamp(value):
    if not value:
        return None
    try:
        if isinstance(value, datetime.datetime):
            stamp = value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
            return stamp.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        pass
    return str(value)


def _decode(value) -> str:
    """Decode store text without inventing lone surrogates.

    utf-16 is only attempted when the bytes look like utf-16: the codec accepts
    unpaired surrogates, so a wrong guess produced text SQLite refuses to store.
    """
    if value is None:
        return ""
    if not isinstance(value, bytes):
        return scrub(str(value))

    candidates: list[str] = []
    if value[:2] in (b"\xff\xfe", b"\xfe\xff"):
        candidates.append("utf-16")
    elif len(value) > 8 and (value[:1] == b"\x00" or value[1:2] == b"\x00"):
        candidates.append("utf-16-le" if value[1:2] == b"\x00" else "utf-16-be")
    candidates += ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    for encoding in candidates:
        try:
            return scrub(value.decode(encoding))
        except (UnicodeDecodeError, LookupError):
            continue
    return scrub(value.decode("latin-1", errors="replace"))


def _attachments(message, include: bool, limit: int, should_stop=None) -> list[dict]:
    if not include:
        return []
    items: list[dict] = []
    try:
        count = message.get_number_of_attachments() or 0
    except Exception:  # noqa: BLE001
        return items
    for index in range(count):
        if should_stop is not None and should_stop():
            break
        if len(items) >= limit:
            break
        try:
            attachment = message.get_attachment(index)
        except Exception:  # noqa: BLE001
            continue
        try:
            name = (attachment.get_long_filename() or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
        name = scrub(name) or f"attachment-{index + 1}"
        data = b""
        try:
            size = attachment.get_size() or 0
            if 0 < size <= MAX_ATTACHMENT_BYTES:
                data = attachment.read_buffer(size) or b""
        except Exception:  # noqa: BLE001 - embedded items and odd payloads
            data = b""
        # The name is kept even when the bytes are not, so the attachment is
        # still findable and its absence is explicit.
        items.append({"name": name, "mime": "", "data": data})
    return items


def _record(
    message,
    folder_path: str,
    include_attachments: bool,
    max_attachments: int,
    should_stop=None,
) -> dict:
    def text(getter):
        try:
            return _decode(getattr(message, getter)())
        except Exception:  # noqa: BLE001
            return ""

    sender = text("get_sender_name")
    subject = text("get_subject")
    delivered = _stamp(text_raw(message, "get_delivery_time"))

    fields: dict[str, str] = {"folder": folder_path}
    if sender:
        fields["from"] = sender.strip()
    if subject:
        fields["subject"] = subject.strip()
    if delivered:
        fields["date"] = delivered

    body = text("get_plain_text_body")
    if not body.strip():
        body = strip_markup(text("get_html_body"))
    body = normalize(body)

    try:
        identifier = message.get_identifier()
    except Exception:  # noqa: BLE001
        identifier = abs(hash((folder_path, subject, delivered)))

    return {
        "uri": f"outlook-store://{folder_path}/{identifier}",
        "name": subject.strip() or "(no subject)",
        "mtime": delivered,
        "fields": fields,
        "body": body,
        "attachments": _attachments(
            message, include_attachments, max_attachments, should_stop
        ),
    }


def text_raw(message, getter):
    try:
        return getattr(message, getter)()
    except Exception:  # noqa: BLE001
        return None


def _walk(folder, path, depth=0):
    name = ""
    try:
        name = (folder.get_name() or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    here = scrub(f"{path}/{name}" if name else path)
    yield folder, here, depth
    if depth >= MAX_FOLDER_DEPTH:
        return
    try:
        count = folder.get_number_of_sub_folders() or 0
    except Exception:  # noqa: BLE001
        return
    for index in range(count):
        try:
            child = folder.get_sub_folder(index)
        except Exception:  # noqa: BLE001
            continue
        yield from _walk(child, here, depth + 1)


def iter_messages(
    store_file: str,
    folder: str | None = None,
    limit: int | None = None,
    include_attachments: bool = True,
    max_attachments: int = 64,
    should_stop=None,
):
    """Yield one record per message in *store_file*."""
    store = _open(store_file)
    emitted = 0
    try:
        root = store.get_root_folder()
        for node, path, _depth in _walk(root, store_file and os.path.basename(store_file) or "store"):
            if should_stop is not None and should_stop():
                return
            if folder is not None and (node.get_name() or "").strip().lower() != folder.lower():
                continue
            try:
                count = node.get_number_of_sub_messages() or 0
            except Exception:  # noqa: BLE001
                continue
            for index in range(count):
                if limit is not None and emitted >= limit:
                    return
                if should_stop is not None and should_stop():
                    return
                try:
                    message = node.get_sub_message(index)
                    record = _record(
                        message,
                        path.lstrip("/"),
                        include_attachments,
                        max_attachments,
                        should_stop,
                    )
                except Exception:  # noqa: BLE001 - skip anything libpff refuses
                    continue
                emitted += 1
                yield record
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass
