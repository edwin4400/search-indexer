"""Stage 1b - incremental change detection.

A polling change source. Portable, dependency-free, and its correctness does not
depend on OS notification semantics: it takes a (size, mtime) snapshot of each
root and diffs consecutive snapshots. The cost is latency - a change is picked
up on the next pass rather than instantly - and latency is the only thing a
native ReadDirectoryChangesW source would improve. The source is isolated behind
`scan()` so that swap touches nothing else.

Partial reads are handled explicitly: when a file is mid-write its read fails,
the attempt is reported as retryable, and the previous signature is kept so the
next pass sees the change again instead of losing it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import RETRY, UNCHANGED, Builder
from .crawl import walk

CREATED = "created"
MODIFIED = "modified"
DELETED = "deleted"


@dataclass(frozen=True)
class Change:
    kind: str
    path: str


def scan(root: str) -> dict[str, tuple[int, str]]:
    """Cheap signature of a tree: path -> (size, mtime). No file is hashed."""
    return {ref.uri: (ref.size, ref.mtime) for ref in walk(root, compute_hash=False)}


def diff(previous: dict, current: dict) -> list[Change]:
    """Changes needed to turn *previous* into *current*, in a stable order."""
    changes: list[Change] = []
    for path, signature in current.items():
        if path not in previous:
            changes.append(Change(CREATED, path))
        elif previous[path] != signature:
            changes.append(Change(MODIFIED, path))
    for path in previous:
        if path not in current:
            changes.append(Change(DELETED, path))
    return sorted(changes, key=lambda change: (change.kind, change.path))


class Watcher:
    """Applies change sets until stopped or until *iterations* passes are done."""

    def __init__(
        self,
        builder: Builder,
        roots,
        interval: float = 2.0,
        verbose: bool = False,
        on_change=None,
        roots_provider=None,
    ):
        self.builder = builder
        self.roots = list(roots)
        # When supplied, the root list is re-read every pass, so a folder added
        # while watching is indexed instead of being ignored until restart.
        self.roots_provider = roots_provider
        self.interval = interval
        self.verbose = verbose
        self.on_change = on_change
        self.passes = 0
        self.applied = 0
        self.changes: list[tuple[Change, str]] = []

    def _adopt_root_changes(self, states: dict) -> None:
        """Pick up roots added or removed since the previous pass."""
        if self.roots_provider is None:
            return
        current = list(self.roots_provider())
        for root in current:
            if root not in states:
                self.builder.log(f"  + root added: {root}")
                # A new root is indexed immediately rather than waiting for a
                # change to appear inside it.
                self.builder.build_roots([root])
                states[root] = scan(root)
        for root in [r for r in states if r not in current]:
            states.pop(root, None)
        self.roots = current

    def _apply(self, change: Change) -> str:
        if change.kind == DELETED:
            outcome = self.builder.remove_path(change.path)
        else:
            outcome = self.builder.index_path(change.path)
        if outcome != UNCHANGED:
            self.applied += 1
        self.changes.append((change, outcome))
        if self.on_change is not None:
            self.on_change(change, outcome)
        elif self.verbose:
            print(f"  {change.kind:<8} {change.path}  -> {outcome}", flush=True)
        return outcome

    def run(
        self,
        iterations: int | None = None,
        settle: float = 0.0,
        stop_event=None,
    ) -> "Watcher":
        """Watch until *iterations* passes are done, or *stop_event* is set.

        The interval sleep is broken into small slices so a stop request is
        honoured promptly instead of after a full interval.
        """
        states = {root: scan(root) for root in self.roots}
        if settle:
            time.sleep(settle)
        while True:
            deadline = time.monotonic() + self.interval
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    return self
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            self._adopt_root_changes(states)
            for root in self.roots:
                if root not in states:
                    continue
                current = scan(root)
                changes = diff(states[root], current)
                new_state = dict(current)
                for change in changes:
                    outcome = self._apply(change)
                    if outcome == RETRY:
                        if change.path in states[root]:
                            new_state[change.path] = states[root][change.path]
                        else:
                            new_state.pop(change.path, None)
                states[root] = new_state
            self.passes += 1
            if iterations is not None and self.passes >= iterations:
                return self
