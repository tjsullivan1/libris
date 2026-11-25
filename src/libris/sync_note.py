"""The note sync leaves beside the Shelf: queued changes that did not apply (#178).

Sync runs unattended, so `typer.echo` reaches nobody, and a rejected Intent has
to reach a person through the Vault instead (ADR 0013). Each run rewrites one
Libris-owned note in the folder that holds the Shelf - never on the Shelf, where
`list_books` would take it for a Book Note.

The note is a rendering, not a store. The remote holds every Intent's state;
the note shows the part a person has to act on:

- **Rejected** Intents, each a task. A rejection is final - sync never tries it
  again - so it stays listed until the person ticks it. The next sync records
  the tick on the Intent as `acknowledged_at` and stops listing it.
- **Waiting** Intents, which the Shelf is holding up. They need no tick: sync
  tries them every run, and once the Shelf is fixed they apply and drop off.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cosmos_store import Container, _sync_lock
from .intents import HOSTED_MCP, IntentOutcome, IntentReport, Settled, _title_of
from .note_format import escape_template_syntax

NOTE_NAME = "Libris sync.md"

_SURFACES = {HOSTED_MCP: "the hosted MCP server"}

# A ticked task naming an Intent. Only `x`: Obsidian themes offer other marks,
# such as `[/]` for "in progress", and those are not a person saying "done".
_TICKED = re.compile(r"^\s*[-*+] \[[xX]\] .*%%\s*intent:([0-9A-Za-z_-]+)\s*%%")

# The line that says libris wrote the note, and so may replace it. A comment,
# so Obsidian shows it only while the cursor is on it.
_MARKER = "%% libris:sync-note - written by libris sync %%"


class NoteRefused(Exception):
    """The note cannot be written where it belongs, and is left as it is.

    Never a reason for sync to stop: the rejections are still on the remote,
    and the push goes ahead without the note.
    """


@dataclass
class NoteReport:
    """What one run did to the note."""

    path: Path
    # Rejections a tick in the note acknowledged this run.
    acknowledged: list[str] = field(default_factory=list)
    # Rejections the note now lists.
    rejected: int = 0
    written: bool = False
    # The note changed between being read and being replaced, so it was left
    # for the next sync.
    changed_while_writing: bool = False


def note_path(vault_path: Path) -> Path:
    """Where the note lives: beside the Shelf, in the folder that holds it."""
    return vault_path.parent / NOTE_NAME


def update_note(
    vault_path: Path, intents: Container, report: IntentReport
) -> NoteReport:
    """Record the ticks in the note, then rewrite it from the remote's state.

    Under the sync lock, so two syncs cannot each read the note the other is
    about to replace.

    Args:
        vault_path: The Shelf. The note is written in the folder holding it.
        intents: The `intents` container.
        report: What this run's sync made of the pending Intents. Its waiting
            Intents are listed; their reasons are not kept on the remote.

    Returns:
        Where the note is, what was acknowledged, and whether it was written.

    Raises:
        NoteRefused: The Shelf is its Vault's root, so beside it is outside
            the Vault, or a file libris did not write already has the note's
            name. Nothing is acknowledged or written.
    """
    path = note_path(vault_path)
    result = NoteReport(path)
    if (vault_path / ".obsidian").is_dir():
        # Beside the Shelf is outside the Vault, where Obsidian neither shows
        # nor syncs it. Not inside the Shelf either: anything that lists Book
        # Notes would find it, and a Bases view's folder filter takes in
        # subfolders too (ADR 0013, #219 review). Only the default config
        # folder is recognised: Obsidian lets it be renamed to any dot-folder,
        # and nothing in one reliably says it is Obsidian's.
        raise NoteRefused(
            f"the Shelf {vault_path} is the root of its Vault, so the note would "
            "sit outside the Vault where Obsidian cannot show it. Move the Book "
            "Notes into a folder of the Vault, and set that folder as the Shelf "
            "with `libris config --vault`"
        )
    with _sync_lock():
        # Read once, so the ticks and the comparison below agree on what the
        # note holds.
        exists, current = _read(path)
        if exists and not _owned(current):
            # A note of the person's own, or one that cannot be read to tell.
            # Its ticks are not ours to act on, and it is not ours to replace
            # (#219 review).
            raise NoteRefused(
                f"{path} was not written by libris, so it was left alone. "
                "Rename or move it, and the next sync writes its own"
            )
        rejected = [
            document
            for document in intents.query_items(
                query="SELECT * FROM c WHERE c.outcome = @outcome",
                parameters=[
                    {"name": "@outcome", "value": IntentOutcome.REJECTED.value}
                ],
                enable_cross_partition_query=True,
            )
            if not document.get("acknowledged_at")
        ]
        ticked = _ticked(current or "")
        outstanding = []
        for document in rejected:
            if document["id"] in ticked:
                intents.upsert_item(
                    {**document, "acknowledged_at": datetime.now(UTC).isoformat()}
                )
                result.acknowledged.append(document["id"])
            else:
                outstanding.append(document)
        outstanding.sort(
            key=lambda d: (_text(d.get("recorded_at")), _text(d.get("id")))
        )
        result.rejected = len(outstanding)

        text = render(outstanding, report.waiting)
        # Only when it changed: a note rewritten every quarter hour is one
        # Obsidian Sync keeps carrying, and one more chance of writing over a
        # tick made on the phone that has not reached this PC yet.
        if text != current:
            # Not if the note changed since it was read: a box ticked on this
            # PC in between would be written over. Left as it is, the next sync
            # reads that tick (#219 review). Narrows the window to the replace
            # itself; nothing short of a lock Obsidian honours closes it.
            if _read(path) != (exists, current):
                result.changed_while_writing = True
                return result
            _write(path, text)
            result.written = True
    return result


def render(rejected: list[dict[str, Any]], waiting: list[Settled]) -> str:
    """The note's text, for these rejected Intents and these waiting ones."""
    lines = [
        _MARKER,
        "# Libris: queued changes that did not apply",
        "",
        "`libris sync` rewrites this note on every run. Ticking a box is the "
        "only edit it keeps.",
        "",
    ]
    if not rejected and not waiting:
        lines.append(
            # Not "everything applied": a rejection ticked off is still one
            # that did not (#219 review).
            "Nothing to act on: no queued change is waiting, and every "
            "rejected one has been ticked off."
        )
        return "\n".join(lines) + "\n"

    lines += ["## Rejected", ""]
    if rejected:
        lines += [
            "Sync will not try these again. If you still want a change, make it "
            "on the Book Note in Obsidian. Then tick it, and the next sync stops "
            "listing it.",
            "",
        ]
        lines += [_rejected_line(document) for document in rejected]
    else:
        lines.append("None: nothing needs deciding.")
    lines.append("")

    if waiting:
        lines += [
            "## Waiting",
            "",
            "Sync tries these again every run, and they leave this list once "
            "they apply. Each says what on the Shelf is holding it up.",
            "",
        ]
        lines += [
            _shown(f"- **{s.title}** ({s.kind}): {s.reason or ''}") for s in waiting
        ]
        lines.append("")
    return "\n".join(lines)


def _rejected_line(document: dict[str, Any]) -> str:
    surface = _text(document.get("surface"))
    asked = _local_day(document.get("recorded_at"))
    origin = f"from {_SURFACES.get(surface, surface or 'an unknown Surface')}"
    if asked:
        origin += f" on {asked}"
    # Flattened whole, not piece by piece: every part but the box is text an
    # Intent carries, and a line break in any of them - a target, a field's
    # name, a Surface - parts the box from its marker, so a tick would never
    # be found (#219 review).
    shown = _shown(
        f"- [ ] **{_title_of(document)}**: {_asked_for(document)}, "
        f"{origin}. Rejected because {_reason(document)}."
    )
    return f"{shown} %%intent:{_one_line(str(document['id']))}%%"


def _reason(document: dict[str, Any]) -> str:
    reason = _one_line(_text(document.get("reason"))).rstrip(".")
    return reason or "no reason was recorded"


def _asked_for(document: dict[str, Any]) -> str:
    """What an Intent asked for, in words, however malformed it is."""
    fields = document.get("fields")
    named = _fields_text(fields) if isinstance(fields, dict) else ""
    kind = document.get("kind")
    if kind == "update":
        target = _text(document.get("target")) or "no Libris ID"
        change = f"set {named}" if named else "change nothing"
        return f"{change} on the note with Libris ID {target}"
    if kind == "add":
        return f"add it, with {named}" if named else "add it"
    return f"a {kind!r} change"


def _fields_text(fields: dict[str, Any]) -> str:
    def value(v: Any) -> str:
        return ", ".join(map(str, v)) if isinstance(v, list) else str(v)

    return "; ".join(f"{k} to {value(v)}" for k, v in fields.items())


def _read(path: Path) -> tuple[bool, str | None]:
    """Whether the note exists, and its text when it can be read as text.

    Bytes that are not UTF-8 are a note that exists but cannot be read: never
    an error that stops a sync before its push (#219 review).
    """
    try:
        return True, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, None
    except UnicodeDecodeError:
        return True, None


def _owned(text: str | None) -> bool:
    """Whether libris wrote this note, so may replace it."""
    return text is not None and _MARKER in text.splitlines()


def _ticked(text: str) -> set[str]:
    """The Intents a person ticked in the note's text."""
    return {match[1] for line in text.splitlines() if (match := _TICKED.match(line))}


def _write(path: Path, text: str) -> None:
    # Written beside and moved over, so Obsidian Sync never carries half a
    # note. A dotfile, so Obsidian never lists the temporary one.
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _local_day(recorded_at: object) -> str:
    if not isinstance(recorded_at, str):
        return ""
    try:
        return datetime.fromisoformat(recorded_at).astimezone().date().isoformat()
    except ValueError:
        return ""


def _one_line(text: str) -> str:
    # One line per Intent, or the tick on it would not be found again.
    return " ".join(text.split())


# A zero-width space, splitting a sequence Obsidian would read as markup while
# leaving it looking the same.
_ZWSP = "​"


def _shown(text: str) -> str:
    """Text an Intent carries, made safe to show as one line of the note.

    Flattened onto one line, and with every way of opening a comment broken
    up: a title holding `%%` or `<!--` would otherwise hide the rest of its
    line, and through the marker's own `%%`, every task listed after it
    (#219 review). And with template syntax escaped, because a title is a
    catalogue's, and this note is created afresh when it was deleted (#231).
    """
    # Every `%` followed by another, not every `%%`: replacing pairs leaves
    # `%%%` holding one. The template opener first, while `<%` is still whole.
    text = escape_template_syntax(_one_line(text))
    return re.sub(r"%(?=%)", f"%{_ZWSP}", text).replace("<!--", f"<{_ZWSP}!--")


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
