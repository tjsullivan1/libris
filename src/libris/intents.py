"""Intents: changes a Surface with no Shelf records, for `libris sync` to apply.

The Shelf is the source of truth (ADR 0002). Whole Book Notes travel up to the
replica; only Intents travel down. The hosted server records one in the
`intents` container where a local Surface would write a note, and the next sync
applies it to the Shelf and records what became of it (ADR 0013). Settled
Intents stay in the container as a record of what each Surface did (ADR 0006).

Two kinds so far: an add carries a whole Book Candidate, and an update a Libris
ID and only the fields it changes (#177).

An Intent is pending until sync settles it, and then carries its Intent Outcome:

- **applied** - the Shelf changed as it asked, and it names the Libris ID it
  made or changed.
- **absorbed** - the Library already held the Book. It names the Libris ID it
  turned out to mean, and the fields it named are set on that note.
- **rejected** - it cannot apply, and a person has to decide: an update naming
  a Libris ID the Shelf no longer holds, or a value the Library does not
  accept. An update is never absorbed.

One that cannot apply *yet* - the Shelf could not be read, say - stays pending
and is tried again by the next sync. That is not an outcome, only a delay.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints

from . import service
from .api import BookCandidate
from .cosmos_store import Container, _sync_lock
from .markdown import FrontmatterUnreadable, NoteNameTaken, NoteWriteFailed

INTENTS = "intents"

# Who recorded an Intent. Only one Surface records them so far; the field is
# there so the record of what each Surface did can say which (ADR 0006).
HOSTED_MCP = "hosted_mcp"

_ADD = "add"
_UPDATE = "update"


class IntentOutcome(Enum):
    """What became of an Intent once sync tried to apply it (ADR 0013)."""

    APPLIED = "applied"
    ABSORBED = "absorbed"
    REJECTED = "rejected"


class IntentQueue:
    """Records Intents in the `intents` container, as the hosted server does."""

    def __init__(self, container: Container, surface: str = HOSTED_MCP) -> None:
        self._container = container
        self._surface = surface

    def record_add(self, candidate: BookCandidate, fields: dict[str, object]) -> str:
        """Record an add-Intent and return its identity.

        The whole candidate travels, as the duplicate check judged it. Sync
        writes the note from it without asking Google Books again, so the note
        is the one that was checked, and a sync never fails on an outage.
        """
        intent_id = uuid.uuid4().hex
        self._container.upsert_item(
            {
                "id": intent_id,
                "kind": _ADD,
                "pending": True,
                "surface": self._surface,
                "recorded_at": datetime.now(UTC).isoformat(),
                "candidate": asdict(candidate),
                "fields": fields,
            }
        )
        return intent_id

    def record_update(
        self, libris_id: str, fields: dict[str, object], title: str | None
    ) -> str:
        """Record an update-Intent and return its identity.

        Only the fields it changes travel (ADR 0002), so applying it cannot
        touch a field the Surface knew nothing about. The title is the
        replica's, for telling a person which Book an Intent meant when it does
        not apply; sync goes by the Libris ID alone.
        """
        intent_id = uuid.uuid4().hex
        self._container.upsert_item(
            {
                "id": intent_id,
                "kind": _UPDATE,
                "pending": True,
                "surface": self._surface,
                "recorded_at": datetime.now(UTC).isoformat(),
                # Not `libris_id`: that is the outcome's, the identity the
                # Intent turned out to mean, written when it settles. A
                # rejected update would lose the one it asked for.
                "target": libris_id,
                "title": title,
                "fields": fields,
            }
        )
        return intent_id


@dataclass
class Settled:
    """One Intent sync settled or put off, as a person is told about it."""

    intent_id: str
    title: str
    libris_id: str | None = None
    reason: str | None = None
    # What the Intent asked for: an add or an update.
    kind: str = _ADD


@dataclass
class IntentReport:
    """What sync made of the pending Intents."""

    applied: list[Settled] = field(default_factory=list)
    absorbed: list[Settled] = field(default_factory=list)
    rejected: list[Settled] = field(default_factory=list)
    # Not settled: tried, and left pending for the next sync.
    waiting: list[Settled] = field(default_factory=list)


def apply_pending(vault_path: Path, intents: Container) -> IntentReport:
    """Apply every pending Intent to the Shelf, oldest first, and settle each.

    Under the sync lock, so two syncs cannot both apply one Intent. Oldest
    first, across adds and updates alike, so of two Intents for one Book the
    first is applied and the second absorbed, and of two updates to one field
    the later one stands - as they would have on the Shelf.

    An Intent is settled after its note is written. A sync that dies in
    between leaves it pending, and the next sync finds the note it wrote and
    absorbs it: the Shelf never gets a second note, though the outcome then
    says absorbed where applied was meant.

    Args:
        vault_path: The Shelf.
        intents: The `intents` container.

    Returns:
        Which Intents were applied, absorbed, rejected or left waiting.
    """
    report = IntentReport()
    with _sync_lock():
        pending = sorted(
            intents.query_items(
                query="SELECT * FROM c WHERE c.pending = true",
                enable_cross_partition_query=True,
            ),
            # Whatever an Intent holds, it sorts: one whose time is not text
            # goes first and is rejected by `_apply_add`, rather than failing
            # the comparison and holding up every Intent and the push (#211).
            key=lambda document: (_text(document.get("recorded_at")), document["id"]),
        )
        # Two notes holding one Libris ID make "the note this Intent meant"
        # ambiguous: absorbing it could set its fields on the other Book, and
        # settle as done. The push refuses such a Shelf, so nothing is applied
        # to it either; every Intent waits until the IDs are repaired (#211
        # review). Checked under the lock, so no Intent is applied to a Shelf
        # that was not checked.
        collisions = service.find_id_collisions(vault_path) if pending else []
        if collisions:
            shared = ", ".join(sorted(c.libris_id for c in collisions))
            reason = f"more than one note holds the Libris ID {shared}"
            report.waiting.extend(
                Settled(
                    document["id"],
                    _title_of(document),
                    reason=reason,
                    kind=_text(document.get("kind")),
                )
                for document in pending
            )
            return report
        for document in pending:
            kind = document.get("kind")
            if kind == _ADD:
                _apply_add(vault_path, intents, document, report)
            elif kind == _UPDATE:
                _apply_update(vault_path, intents, document, report)
            else:
                # Recorded by a newer server than this sync. Rejecting it would
                # throw away a change nothing here understood; it waits, and
                # the reason says what fixes it.
                report.waiting.append(
                    Settled(
                        document["id"],
                        _title_of(document),
                        reason=(
                            f"this libris cannot apply a {kind!r} change; "
                            "update libris on this computer"
                        ),
                        kind=_text(kind),
                    )
                )
    return report


def _apply_update(
    vault_path: Path,
    intents: Container,
    document: dict[str, Any],
    report: IntentReport,
) -> None:
    """Apply an update-Intent to the note its Libris ID means, or settle why not.

    Applied when the note is found, following a Superseded ID to the note that
    absorbed it (ADR 0014). Rejected when the Shelf holds no note for the ID, or
    a value is not one the Library accepts (ADR 0013). Waits on anything the
    Shelf could fix by itself.
    """
    title = _title_of(document)

    def settled(**kwargs: Any) -> Settled:
        return Settled(document["id"], title, kind=_UPDATE, **kwargs)

    try:
        target = _target(document.get("target"))
        fields = _fields(document.get("fields"))
        if not fields:
            raise ValueError("it names no field to change")
        asked = _local_day(document.get("recorded_at"))
    except (TypeError, ValueError) as error:
        _settle(
            intents,
            document,
            IntentOutcome.REJECTED,
            report.rejected,
            settled(reason=f"it cannot be read: {error}"),
        )
        return

    try:
        # Dated the day it was asked, as a queued add is (#214).
        result = service.update_book(vault_path, target, fields, today=asked)
    # Before BookNotFound, which it is: the note was found, then renamed or
    # replaced before the write. It is likely still there, so this waits.
    except (
        service.NoteMoved,
        service.ShelfUnreadable,
        FrontmatterUnreadable,
        NoteWriteFailed,
        OSError,
    ) as error:
        report.waiting.append(settled(reason=str(error)))
        return
    except service.BookNotFound:
        # The Shelf, read whole, holds no note for it, and no note supersedes
        # it. Only a person can say which Book was meant (ADR 0013).
        _settle(
            intents,
            document,
            IntentOutcome.REJECTED,
            report.rejected,
            settled(reason=f"no note on the Shelf holds the Libris ID {target}"),
        )
        return
    except ValueError as error:
        # A value the Library does not accept. Judged when it was recorded, so
        # only a vocabulary changed since can reach here. Nothing is written:
        # the fields are judged before the note is opened.
        _settle(
            intents,
            document,
            IntentOutcome.REJECTED,
            report.rejected,
            settled(reason=str(error)),
        )
        return

    _settle(
        intents,
        document,
        IntentOutcome.APPLIED,
        report.applied,
        # The note's own identity, which for a superseded target is the
        # survivor's: what the replica should have called it.
        Settled(
            document["id"],
            result.note.title or title,
            libris_id=result.note.libris_id or target,
            kind=_UPDATE,
        ),
    )


def _apply_add(
    vault_path: Path,
    intents: Container,
    document: dict[str, Any],
    report: IntentReport,
) -> None:
    raw = document.get("candidate")
    title = _title_of(document)
    try:
        # Everything an Intent holds is judged here, before any of it is used.
        # One this code cannot make sense of is rejected; one that crashed
        # further on would stay pending and stop every later sync (#211).
        candidate = _candidate(raw)
        fields = _fields(document.get("fields"))
        added = _local_day(document.get("recorded_at"))
    except (KeyError, TypeError, ValueError) as error:
        # Written by a server this code does not match. Nothing here can make
        # sense of it, and leaving it pending would try again forever.
        _settle(
            intents,
            document,
            IntentOutcome.REJECTED,
            report.rejected,
            Settled(document["id"], title, reason=f"it cannot be read: {error}"),
        )
        return

    try:
        result = service.add_book(
            vault_path, candidate, overrides=fields or None, added=added
        )
        libris_id = result.libris_id
        if (
            libris_id is not None
            and result.outcome is service.Outcome.ALREADY_PRESENT
            and fields
        ):
            # Absorbed, and the Intent's fields still apply: they name only
            # what it changes, so setting them on the note it meant is what it
            # asked for (ADR 0013). By identity, checked inside the write: a
            # file replaced since the match is not the Book it meant, and that
            # raises BookNotFound and waits rather than editing another Book
            # (#211 review). The ID recorded is the note's that was written.
            # Dated the day it was asked, as a new note is: the day sync ran
            # is not a day anyone finished anything (#214).
            libris_id = service.update_book(
                vault_path, libris_id, fields, today=added
            ).note.libris_id
    # The Shelf, not the Intent: a locked note, a name another note holds,
    # frontmatter an edit broke. Rejecting would hand a person a decision that
    # fixing the Shelf makes for them, so it waits for the next sync. Caught
    # before ValueError, which FrontmatterUnreadable is.
    except (
        service.ShelfUnreadable,
        service.BookNotFound,
        FrontmatterUnreadable,
        NoteNameTaken,
        NoteWriteFailed,
        OSError,
    ) as error:
        report.waiting.append(Settled(document["id"], title, reason=str(error)))
        return
    except ValueError as error:
        # A value the Library does not accept (InvalidFieldValue is one).
        # Checked when it was recorded, so only a vocabulary that changed since
        # can reach here.
        _settle(
            intents,
            document,
            IntentOutcome.REJECTED,
            report.rejected,
            Settled(document["id"], title, reason=str(error)),
        )
        return

    if libris_id is None:
        # An outcome names the Libris ID the Intent made or meant (ADR 0013),
        # and a settled Intent is never looked at again. A note written but
        # locked before it could be read back, or a held note with no Libris
        # ID yet, has none to give, so it waits: the next sync finds the note
        # and absorbs the Intent into it (#211).
        report.waiting.append(
            Settled(
                document["id"],
                result.title or title,
                reason="the note it means has no Libris ID that could be read yet",
            )
        )
        return
    if result.outcome is service.Outcome.CREATED:
        outcome, into = IntentOutcome.APPLIED, report.applied
    else:
        outcome, into = IntentOutcome.ABSORBED, report.absorbed
    _settle(
        intents,
        document,
        outcome,
        into,
        Settled(document["id"], result.title or title, libris_id=libris_id),
    )


def _title_of(document: dict[str, Any]) -> str:
    """What an Intent calls its Book, however malformed the Intent.

    An add carries its candidate's title; an update, the replica's title for
    the note it named, and failing that the Libris ID it named.
    """
    raw = document.get("candidate")
    return (
        _text(raw.get("title") if isinstance(raw, dict) else None)
        or _text(document.get("title"))
        or _text(document.get("target"))
        or "an unnamed book"
    )


def _target(raw: object) -> str:
    """The Libris ID an update-Intent names, or why it names none."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"target is {raw!r}, not a Libris ID")
    return raw.strip()


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _candidate(raw: object) -> BookCandidate:
    """The candidate an Intent carries, or why it is not one."""
    if not isinstance(raw, dict):
        raise TypeError(f"candidate is {type(raw).__name__}, not a mapping")
    candidate = BookCandidate(**raw)
    # Every field, against the type the candidate declares for it: one of the
    # wrong type crashes matching or the note's writing further on, where it
    # would stay pending and stop every later sync (#211 review).
    for name, hint in get_type_hints(BookCandidate).items():
        value = getattr(candidate, name)
        if not _conforms(value, hint):
            raise ValueError(f"candidate {name} is {value!r}")
    # The name a note is filed and matched under.
    if not candidate.title.strip():
        raise ValueError(f"candidate title is {candidate.title!r}")
    return candidate


def _conforms(value: object, hint: Any) -> bool:
    """Whether `value` is of the type `hint` names, for the types a candidate
    declares: plain classes, `X | None`, and `list[X]`."""
    if isinstance(hint, UnionType):
        return any(_conforms(value, option) for option in get_args(hint))
    if hint is type(None):
        return value is None
    if get_origin(hint) is list:
        (item,) = get_args(hint)
        return isinstance(value, list) and all(_conforms(v, item) for v in value)
    # A bool is an int to isinstance, but never a page count.
    return isinstance(value, hint) and not (hint is int and isinstance(value, bool))


def _fields(raw: object) -> dict[str, object]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError(f"fields are {type(raw).__name__}, not a mapping")
    return dict(raw)


def _local_day(recorded_at: object) -> date:
    """The day an Intent was recorded, on this PC's calendar.

    Recorded in UTC, because the server has no calendar of its own. The Libris
    ID is minted from this day (ADR 0011), and the person asked on their own.
    """
    if not isinstance(recorded_at, str):
        raise ValueError(f"recorded_at is {recorded_at!r}, not a time")
    return datetime.fromisoformat(recorded_at).astimezone().date()


def _settle(
    intents: Container,
    document: dict[str, Any],
    outcome: IntentOutcome,
    into: list[Settled],
    settled: Settled,
) -> None:
    intents.upsert_item(
        {
            **document,
            "pending": False,
            "outcome": outcome.value,
            "libris_id": settled.libris_id,
            "reason": settled.reason,
            "settled_at": datetime.now(UTC).isoformat(),
        }
    )
    into.append(settled)
