#!/usr/bin/env python3
"""Launcher - start the local service and open the app in a browser.

    python run.py                    # default database, port 8765
    python run.py --no-browser
    python run.py --db index\\app.db --port 8801
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def ensure_dir(path: str) -> str:
    """Create a folder or explain clearly why it cannot be used."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"cannot use {path} for the index: {exc}\n"
            "  Pass --data-dir <folder> to keep the index somewhere writable."
        ) from exc
    return path


def redirect_output() -> None:
    """A windowed build has no console, so print() would write to a null stream.

    Everything the console used to show goes to a log file in the data folder,
    which is also where a startup failure is recorded.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        stream = open(os.path.join(data_dir(), "content-search.log"),
                      "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def alert(message: str) -> None:
    """Say something when there is no console to say it in."""
    if not frozen():
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Content Search", 0x10)
    except Exception:  # noqa: BLE001 - a missing dialog must not mask the error
        pass


def data_dir() -> str:
    """Where a built executable keeps its index and settings.

    Per-user application data, because the folder holding a distributed .exe may
    be read-only, on a network share, or under Program Files. Running from the
    repository keeps everything beside the code.
    """
    return ensure_dir(os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ContentSearch"
    ))

from app.server import serve  # noqa: E402


def main(argv=None) -> int:
    redirect_output()
    parser = argparse.ArgumentParser(description="content search app")
    parser.add_argument("--db", default=None, help="index database; default follows --data-dir")
    parser.add_argument("--config", default=None, help="settings file; default follows --data-dir")
    parser.add_argument("--data-dir", default=None,
                        help="keep the index and settings here, for a portable install")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.data_dir or frozen():
        # A distributed executable must not assume its own folder is writable,
        # and may be run from a read-only share or a USB stick.
        base = ensure_dir(os.path.abspath(args.data_dir) if args.data_dir else data_dir())
        db_path = args.db or os.path.join(base, "index", "app.db")
        config_path = args.config or os.path.join(base, "config.json")
    else:
        db_path = args.db or os.path.join(HERE, "index", "app.db")
        config_path = args.config or os.path.join(HERE, "app", "config.json")

    serve(
        db_path=db_path,
        config_path=config_path,
        port=args.port,
        open_browser=not args.no_browser,
        verbose=args.verbose,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        # Without a console, a startup refusal (another instance, busy port)
        # would look like nothing happened at all.
        if isinstance(exc.code, str):
            alert(exc.code)
        raise
