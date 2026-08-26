from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    checks: dict[str, bool]
