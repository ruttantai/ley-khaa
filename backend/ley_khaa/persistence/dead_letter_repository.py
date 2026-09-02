"""Where a dropped message goes so it is not simply gone (spec §3.8, §7).

This is the first part of the system that handles credentials, and a dead
letter is the one place a raw platform payload would otherwise be written to
disk verbatim — Slack's own Socket Mode envelope carries a `token` field. So
redaction is not a nicety here; it is the reason this module owns payload
serialisation rather than letting callers pass a pre-rendered string.
"""
from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from .orm import DeadLetterRow

# A payload is a diagnostic, not an archive. Big enough to hold a whole Slack
# event, small enough that no single row is large.
#
# This bounds row SIZE, not row COUNT. Row COUNT is bounded separately by
# `settings.dead_letter_max_rows` (backlog item 18) — see `record()`'s prune
# below. A permanently bad token still makes the supervisor crash-loop at its
# 60s backoff cap, writing one `connection` row per minute for as long as it
# runs, and that stays visible in the dashboard by design — a silent drop is
# the failure this table exists to prevent — but it no longer grows without
# bound.
MAX_PAYLOAD_CHARS = 4_000

_TRUNCATED = "…[truncated]"

# Substring match, case-folded: a credential added later ("app_token",
# "SLACK_SIGNING_SECRET") is caught by default rather than leaking until
# somebody notices it in the dashboard.
_SECRET_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "api_key",
    "apikey",
    "signing",
    "signature",
    "credential",
)

REDACTED = "[redacted]"

# Anything json can render as-is. Everything else is replaced by its TYPE, never
# its repr: repr() is attacker-controlled surface — an SDK client, a session, or
# an exception routinely embeds the credential (or the whole authenticated
# request) that produced it, and json.dumps(default=repr) would render exactly
# that. A payload is a diagnostic, not an archive, so losing detail here is the
# right trade.
_RENDERABLE = (str, int, float, bool, type(None))

# Credential shapes that appear in free text, where key-based redaction cannot
# see them — an SDK exception's message routinely quotes the request that
# produced it. Prefix-anchored rather than entropy-guessing: a false positive
# silently destroys a diagnostic, so only well-known shapes match.
#
# The Bearer tail requires 16+ characters BECAUSE the obvious pattern
# (`bearer\s+\S+`) eats ordinary English: "missing bearer token in the request"
# is a realistic auth diagnostic carrying no secret, and redacting it destroys
# the very information the dead letter exists to preserve. Real bearer tokens
# are long; the English words that follow "bearer" are not.
_MIN_BEARER_TOKEN = 16

_TOKEN_PATTERNS = (
    re.compile(r"(?i)xox[abposr]-[A-Za-z0-9-]+"),           # Slack bot/user/app tokens
    re.compile(r"(?i)xapp-[A-Za-z0-9-]+"),                  # Slack app-level tokens
    re.compile(rf"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{{{_MIN_BEARER_TOKEN},}}"),  # Authorization headers
)


def scrub_text(value: str) -> str:
    """Replace credential shapes inside free text.

    Applies to `reason` as well as payload strings: key-based redaction only
    sees `{"token": ...}`, and a platform SDK's exception message embeds the
    credential in a sentence instead.
    """
    for pattern in _TOKEN_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _scrub(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_secret(key) else _scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if isinstance(value, _RENDERABLE):
        return scrub_text(value) if isinstance(value, str) else value
    return f"<{type(value).__name__}>"


def _is_secret(key: object) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def redact(payload: object) -> str:
    """Serialise `payload` with every credential-shaped key replaced.

    Never raises: a payload that will not serialise is described instead, since
    a dead letter records a failure and must not become one. After `_scrub`,
    every value handed to `json.dumps` is already a container or a JSON
    primitive, so `default=repr` should not fire in the normal path — it stays
    as a belt-and-braces fallback in case some renderable type slips past
    `_RENDERABLE` in the future. The `try`/`except` is the guard that actually
    matters: `_scrub` itself can raise (a self-referential container hits
    Python's own `RecursionError`), and `json.dumps` can still refuse a
    scrubbed structure it does not like (a non-string dict key, for instance),
    so this catches both routes rather than only the one `default=repr` was
    ever meant for.
    """
    if payload is None:
        return ""
    try:
        text = json.dumps(_scrub(payload), default=repr, ensure_ascii=False)
    except Exception:
        text = f"<unserialisable payload: {type(payload).__name__}>"
    if len(text) > MAX_PAYLOAD_CHARS:
        text = text[: MAX_PAYLOAD_CHARS - len(_TRUNCATED)] + _TRUNCATED
    return text


class DeadLetterRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self, *, source: str, kind: str, reason: str, payload: object = None
    ) -> DeadLetterRow:
        row = DeadLetterRow(
            id=str(uuid.uuid4()),
            source=source,
            kind=kind,
            reason=scrub_text(reason),
            payload=redact(payload),
        )
        self.session.add(row)
        self._prune()
        self.session.commit()
        self.session.refresh(row)
        return row

    def _prune(self) -> None:
        """Enforce `settings.dead_letter_max_rows`, oldest rows first.

        Runs in the same flush/commit as the write in `record()` — see the
        single `commit()` after this call — so a crash between insert and
        prune cannot leave the table permanently over the cap; the next
        successful write prunes it back down.

        Ordered newest-first with the same (`created_at`, `id`) tiebreak
        `list()` uses, then everything past the cap is deleted: those are, by
        construction, the OLDEST rows, which is what makes the newest ones
        the survivors.
        """
        self.session.flush()
        keep_ids = (
            select(DeadLetterRow.id)
            .order_by(DeadLetterRow.created_at.desc(), DeadLetterRow.id.desc())
            .limit(settings.dead_letter_max_rows)
        )
        self.session.execute(
            delete(DeadLetterRow).where(DeadLetterRow.id.not_in(keep_ids))
        )

    def list(self, limit: int = 100) -> list[DeadLetterRow]:
        """Newest first — the dashboard shows the most recent drop at the top.

        Ordered by created_at AND id, not created_at alone. The timestamps are
        microsecond-resolution, so a tie is rare rather than routine — but
        "rare" is exactly the kind of ordering a test discovers on someone
        else's machine months later. The id tiebreak makes the order total and
        deterministic instead of leaving equal timestamps to whatever the
        database happened to return.
        """
        return list(
            self.session.scalars(
                select(DeadLetterRow)
                .order_by(DeadLetterRow.created_at.desc(), DeadLetterRow.id.desc())
                .limit(limit)
            )
        )
