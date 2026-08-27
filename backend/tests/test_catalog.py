import csv
import io

from ley_khaa.executor import catalog


def _rows(name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(catalog.build_dataset(name))))


def test_datasets_are_deterministic():
    """The whole reproducibility claim rests on this: same seed, same bytes."""
    catalog.build_dataset.cache_clear()
    first = catalog.build_dataset("bloomberg_universe")
    catalog.build_dataset.cache_clear()
    second = catalog.build_dataset("bloomberg_universe")
    assert first == second


def test_universes_differ_in_both_directions():
    """The demo asks what is missing; a set-difference with nothing to find
    would make the headline conversation look like it worked when it didn't."""
    bloomberg = {r["ticker"] for r in _rows("bloomberg_universe")}
    factset = {r["ticker"] for r in _rows("factset_universe")}
    assert len(bloomberg - factset) == 5
    assert len(factset - bloomberg) == 3


def test_resolve_name_matches_a_spoken_input_name():
    assert catalog.resolve_name("Bloomberg universe") == "bloomberg_universe"
    assert catalog.resolve_name("factset") == "factset_universe"
    assert catalog.resolve_name("holdings") == "holdings"


def test_resolve_name_refuses_to_guess_when_ambiguous():
    assert catalog.resolve_name("universe") is None


def test_resolve_name_returns_none_for_unknown_input():
    assert catalog.resolve_name("trades") is None
    assert catalog.resolve_name("") is None
