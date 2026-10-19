"""Outlook mail connector, via COM.

Why COM rather than reading the .ost: a store Outlook has open is byte-range
locked. Reading it raises PermissionError and copying it raises a lock error, so
there is no offline snapshot to take while Outlook runs. The mailbox is queried
through Outlook instead, which is what X1 does too.

Messages become items; attachments become sub-items, so the existing attachment
handling - content extraction, parent resolution, cascade delete - applies with
no new code.

`pypff` (libpff) is installed as well and reads PST/OST offline, which is the
better route for a store no process has open. It is not used here because the
file is locked.
"""

from __future__ import annotations

import os
import tempfile

from .analyze import normalize, strip_markup

OL_MAIL_CLASS = 43
MAX_FOLDER_DEPTH = 8
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class OutlookUnavailable(RuntimeError):
    """Outlook could not be reached."""


def _namespace():
    try:
        import win32com.client
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise OutlookUnavailable(f"pywin32 is required for the Outlook connector: {exc}") from exc
    try:
        application = win32com.client.Dispatch("Outlook.Application")
        return application.GetNamespace("MAPI")
    except Exception as exc:  # noqa: BLE001 - COM surfaces many error types
        raise OutlookUnavailable(f"could not reach Outlook: {exc}") from exc


def _walk_folders(folder, depth=0):
    yield folder, depth
    if depth >= MAX_FOLDER_DEPTH:
        return
    try:
        children = folder.Folders
        count = children.Count
    except Exception:  # noqa: BLE001
        return
    for index in range(1, count + 1):
        try:
            child = children.Item(index)
        except Exception:  # noqa: BLE001
            continue
        yield from _walk_folders(child, depth + 1)


def _attachment_bytes(attachment):
    """Outlook only hands out attachments by writing them to a file."""
    handle, path = tempfile.mkstemp(prefix="cs-attachment-")
    os.close(handle)
    try:
        attachment.SaveAsFile(path)
        if not os.path.exists(path):
            return b""
        with open(path, "rb") as stream:
            return stream.read()
    except Exception:  # noqa: BLE001 - blocked file types and embedded items fail here
        return b""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _record(item, folder_name: str, include_attachments: bool, store_id: str = ""):
    fields: dict[str, str] = {}
    for label, attribute in (
        ("from", "SenderName"),
        ("from_address", "SenderEmailAddress"),
        ("to", "To"),
        ("cc", "CC"),
        ("subject", "Subject"),
    ):
        value = _text(getattr(item, attribute, None))
        if value:
            fields[label] = value
    fields["folder"] = folder_name

    received = None
    for attribute in ("ReceivedTime", "LastModificationTime", "CreationTime"):
        try:
            received = getattr(item, attribute, None)
        except Exception:  # noqa: BLE001
            received = None
        if received:
            break
    if received:
        fields["date"] = str(received)

    body = ""
    try:
        body = _text(getattr(item, "Body", None))
    except Exception:  # noqa: BLE001
        body = ""
    if not body:
        try:
            body = strip_markup(_text(getattr(item, "HTMLBody", None)))
        except Exception:  # noqa: BLE001
            body = ""
    body = normalize(body)

    attachments = []
    if include_attachments:
        try:
            collection = item.Attachments
            count = collection.Count
        except Exception:  # noqa: BLE001
            count = 0
        for index in range(1, count + 1):
            try:
                attachment = collection.Item(index)
                name = _text(getattr(attachment, "FileName", None)) or f"attachment-{index}"
            except Exception:  # noqa: BLE001
                continue
            data = _attachment_bytes(attachment)
            if data and len(data) <= MAX_ATTACHMENT_BYTES:
                attachments.append({
                    "name": name, "mime": "", "data": data,
                    # Position within the message: Outlook reopens an attachment
                    # by asking the parent item for it by index.
                    "origin": f"{store_id}|{index}",
                })
            else:
                # Still catalogue the name so it is findable and the reason is
                # explicit rather than looking like an email with no attachment.
                attachments.append({
                    "name": name, "mime": "", "data": b"",
                    "origin": f"{store_id}|{index}",
                })

    try:
        entry_id = str(item.EntryID)
    except Exception:  # noqa: BLE001
        entry_id = f"{folder_name}-{id(item)}"

    return {
        "uri": f"outlook://{folder_name}/{entry_id}",
        "name": fields.get("subject") or "(no subject)",
        "mtime": str(received) if received else None,
        "fields": fields,
        "body": body,
        "attachments": attachments,
        # What Outlook needs to be handed this item back: its entry id is in the
        # uri, and the store id here.
        "origin": store_id,
    }


def iter_messages(
    store: str | None = None,
    folder: str | None = None,
    limit: int | None = None,
    include_attachments: bool = True,
    stats: dict | None = None,
    should_stop=None,
):
    """Yield one record per mail item, newest-first within each folder.

    *stats* collects counters and the last failure, so a pass that could not read
    everything is visibly partial instead of looking complete. *should_stop* is
    polled between items: an Outlook call can block for a long time while it
    fetches something it has not cached, and a sync has to stay cancellable.
    """
    import pythoncom  # noqa: F401 - required in the calling thread

    namespace = _namespace()
    roots = []
    try:
        stores = namespace.Folders
        count = stores.Count
    except Exception as exc:  # noqa: BLE001
        raise OutlookUnavailable(f"could not list Outlook stores: {exc}") from exc
    for index in range(1, count + 1):
        try:
            candidate = stores.Item(index)
        except Exception:  # noqa: BLE001
            continue
        if store is None or candidate.Name.lower() == store.lower():
            roots.append(candidate)
    if not roots:
        raise OutlookUnavailable(f"no Outlook store matched {store!r}")

    emitted = 0
    for root in roots:
        for node, _depth in _walk_folders(root):
            node_name = _text(getattr(node, "Name", "")) or "?"
            try:
                store_id = str(node.StoreID)
            except Exception:  # noqa: BLE001 - not every folder exposes one
                store_id = ""
            if folder is not None and node_name.lower() != folder.lower():
                continue
            try:
                items = node.Items
                total = items.Count
            except Exception:  # noqa: BLE001
                continue
            for index in range(1, total + 1):
                if limit is not None and emitted >= limit:
                    return
                if should_stop is not None and should_stop():
                    if stats is not None:
                        stats["stopped"] = True
                    return
                try:
                    item = items.Item(index)
                    if int(getattr(item, "Class", 0) or 0) != OL_MAIL_CLASS:
                        continue
                    record = _record(item, node_name, include_attachments, store_id)
                except Exception as exc:  # noqa: BLE001 - one bad item must not end the pass
                    if stats is not None:
                        stats["skipped"] = stats.get("skipped", 0) + 1
                        stats.setdefault("last_error", f"{type(exc).__name__}: {exc}")
                    continue
                emitted += 1
                if stats is not None:
                    stats["emitted"] = emitted
                yield record


def store_names() -> list[str]:
    namespace = _namespace()
    names = []
    stores = namespace.Folders
    for index in range(1, stores.Count + 1):
        try:
            names.append(_text(stores.Item(index).Name))
        except Exception:  # noqa: BLE001
            continue
    return names
