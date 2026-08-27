"""Faker-seeded synthetic securities datasets (spec §5.10, decision 2).

The catalog is the fallback when a spec input name matches no attachment. It is
deterministic on purpose: the same seed produces the same rows on every machine
and every run, which is what lets the reproducibility test make a claim about
the bundle rather than about the weather.

All data here is synthetic. Nothing in this module touches a real vendor feed.
"""
from __future__ import annotations

import csv
import io
import re
from functools import lru_cache

from faker import Faker

CATALOG_SEED = 20260825

DATASET_NAMES = ("bloomberg_universe", "factset_universe", "holdings", "portfolio")

_SECTORS = ("Financials", "Technology", "Energy", "Healthcare", "Industrials")
_CURRENCIES = ("USD", "EUR", "GBP", "JPY")

_UNIVERSE_SIZE = 200
# factset drops the first few rows of the shared base and gains a tail of its
# own, so the demo's set-difference finds something in BOTH directions.
_FACTSET_DROPPED = 5
_FACTSET_EXTRA = 3

_UNIVERSE_FIELDS = ["ticker", "isin", "name", "sector", "currency"]
_POSITION_FIELDS = ["ticker", "isin", "quantity", "weight"]

_TOKEN = re.compile(r"[a-z0-9]+")


def _base_rows() -> list[dict[str, str]]:
    fake = Faker()
    Faker.seed(CATALOG_SEED)
    rows: list[dict[str, str]] = []
    for i in range(_UNIVERSE_SIZE + _FACTSET_EXTRA):
        rows.append(
            {
                "ticker": f"SYN{i:04d}",
                "isin": f"XS{i:010d}",
                "name": fake.company(),
                "sector": _SECTORS[i % len(_SECTORS)],
                "currency": _CURRENCIES[i % len(_CURRENCIES)],
                "quantity": str((i * 37) % 5000 + 100),
                "weight": f"{(i % 97 + 1) / 1000:.4f}",
            }
        )
    return rows


def _to_csv(rows: list[dict[str, str]], fields: list[str]) -> str:
    buf = io.StringIO()
    # lineterminator is pinned so the bytes do not differ between platforms.
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row[field] for field in fields})
    return buf.getvalue()


@lru_cache(maxsize=None)
def build_dataset(name: str) -> str:
    """Return the dataset as CSV text. Raises KeyError for an unknown name."""
    if name not in DATASET_NAMES:
        raise KeyError(name)
    rows = _base_rows()
    if name == "bloomberg_universe":
        return _to_csv(rows[:_UNIVERSE_SIZE], _UNIVERSE_FIELDS)
    if name == "factset_universe":
        return _to_csv(rows[_FACTSET_DROPPED:], _UNIVERSE_FIELDS)
    if name == "holdings":
        return _to_csv(rows[:60], _POSITION_FIELDS)
    return _to_csv(rows[3:63], _POSITION_FIELDS)


def tokens(value: str) -> frozenset[str]:
    """Extract normalized tokens from a string for matching.

    Shared with the resolver so a spec input name and an attachment filename are
    always tokenized the same way; two tokenizers would drift.
    """
    return frozenset(_TOKEN.findall(value.lower()))


def resolve_name(query: str) -> str | None:
    """Map a spec input name onto a dataset, or None if unknown or ambiguous.

    Ambiguity resolves to None deliberately: "universe" matches two datasets,
    and guessing which one the human meant is exactly the mistake that should
    become a clarification instead of a silently wrong answer.

    Matches only if all query tokens are present in the dataset name tokens;
    this prevents "holdings screenshot" from matching "holdings".
    """
    wanted = tokens(query)
    if not wanted:
        return None
    matches = [
        name for name in DATASET_NAMES
        if wanted <= tokens(name)
    ]
    return matches[0] if len(matches) == 1 else None
