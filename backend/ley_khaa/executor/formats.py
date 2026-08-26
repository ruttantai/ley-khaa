"""Mapping between the words a request uses for an output and a file suffix.

Shared by the synthesizer (which tells the script what to write) and the
validator (which checks what it wrote), so the two can never disagree about
what "Excel" means.
"""
from __future__ import annotations

_SUFFIXES: dict[str, tuple[str, ...]] = {
    "xlsx": (".xlsx",),
    "excel": (".xlsx",),
    "spreadsheet": (".xlsx",),
    "csv": (".csv",),
    "docx": (".docx",),
    "word": (".docx",),
    "markdown": (".md",),
    "md": (".md",),
    "json": (".json",),
    "text": (".txt",),
}


def expected_suffixes(output_format: str) -> tuple[str, ...]:
    """Suffixes that satisfy this format. Empty means "no opinion".

    An unrecognised format must NOT fail validation: rejecting a perfectly good
    deliverable because the request described it in words we did not anticipate
    is worse than not checking.
    """
    return _SUFFIXES.get((output_format or "").strip().lower(), ())


def deliverable_filename(output_format: str) -> str:
    suffixes = expected_suffixes(output_format)
    return f"output{suffixes[0]}" if suffixes else "output.txt"
