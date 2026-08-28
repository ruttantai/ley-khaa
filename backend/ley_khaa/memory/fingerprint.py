"""Have I been asked this before? — the deterministic half (spec §3.5).

A token SET, not a sequence: a repeat request is rarely typed identically, and
word order carries no meaning here. Paraphrase beyond this is stage 2's job.
"""
from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"[a-z0-9]+")

# Pinned by a test. Adding a word re-fingerprints every stored memory, so every
# request remembered before the change silently stops matching — a cache that
# forgets everything without saying so.
STOPWORDS = frozenset(
    """
    a an the this that these those and or but if then so as of to in on for with
    at by from is are was were be been being do does did can could would should
    will please thanks thank hi hello hey we i you it me my our your
    """.split()
)


def request_fingerprint(texts: list[str]) -> str:
    """The significant tokens of a request, sorted and hashed.

    Bare numbers are dropped so that a date does not split "the usual universe
    check" into a new request every time it runs.
    """
    tokens = {
        token
        for text in texts
        for token in _TOKEN.findall((text or "").lower())
        if token not in STOPWORDS and not token.isdigit()
    }
    if not tokens:
        # Never let two blank requests fingerprint together — an empty hash
        # would match every other empty one and hand them each other's spec.
        return ""
    return hashlib.sha256(" ".join(sorted(tokens)).encode("utf-8")).hexdigest()
