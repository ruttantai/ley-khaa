import re
from typing import TypeVar

from pydantic import BaseModel

from ..crystallizer.engine import HANDLED_HEADER, CandidateDraft, CrystallizerOutput
from ..crystallizer.relevance import RelevanceVerdict
from ..executor.synthesizer import SynthesizedScript
from ..interpreter.spec import TaskSpec
from ..memory.models import MemoryDecision
from ..registry.models import RegistryDecision
from .router import ModelChoice

T = TypeVar("T", bound=BaseModel)

_REQUEST_WORDS = (
    "compare", "reconcile", "send", "pull", "report", "check", "build",
    "export", "summar", "difference", "missing", "list", "generate",
)
_NOISE_PATTERNS = (
    r"^\s*(hi|hey|hello|morning|thanks|thank you|ok|okay|cool|nice|lol|haha|sure)\b",
    r"^\s*\W*$",
)

_MESSAGE_LINE = re.compile(r"^\[(?P<id>[^\]]+)\]\s+(?P<author>[^:]+):\s*(?P<text>.*)$")
_OWNS = re.compile(r"owns=\[(?P<ids>[^\]]*)\]")

_OPERATIONS = (
    (("compare", "difference", "missing", "reconcile", "against"), "set_difference"),
    (("summar", "stats", "group by", "breakdown", "average"), "summary_stats"),
)
_FORMATS = (
    (("excel", "xlsx", "spreadsheet"), "xlsx"),
    (("csv",), "csv"),
    (("word", "docx"), "docx"),
    (("markdown", "md"), "markdown"),
)
# Longest first. A shorter name whose words a longer match already covered is
# dropped: "bloomberg universe" is one dataset, and also emitting the bare word
# "universe" yields an input that matches TWO catalog datasets, which the
# resolver correctly refuses to guess between (executor/catalog.py).
_SOURCE_PHRASES = (
    "bloomberg universe",
    "factset universe",
    "bloomberg",
    "factset",
    "holdings",
    "portfolio",
    "trades",
    "universe",
)
_URGENT_WORDS = ("urgent", "asap", "right away", "eod", "immediately")
_RECIPIENT = re.compile(r"send (?:it |them |this )?to (?P<who>[a-z][\w.-]*)")

# Deliberately mediocre: a regex has not understood anything, and the autonomy
# engine must never hand Auto to keyword matching. See the threshold in
# ley_khaa/autonomy/engine.py.
_HEURISTIC_CERTAINTY = 0.55

# The prompt the Synthesizer builds (executor/synthesizer.py::_task_block).
_SYNTH_OPERATION = re.compile(r"^operation:\s*(?P<operation>.+)$", re.MULTILINE)
_SYNTH_TARGET = re.compile(r"^write the result to:\s*(?P<target>\S+)$", re.MULTILINE)
_SYNTH_INPUT = re.compile(r"^### inputs/(?P<filename>\S+)", re.MULTILINE)

_PREAMBLE = '''"""Offline canned generator.

Written by ley-khaa's deterministic offline stand-in, not by a model. It is a
real, runnable program: it reads the frozen inputs and writes the deliverable,
so a fresh clone with no ANTHROPIC_API_KEY still produces a genuine bundle.
"""
import csv
import json

with open("inputs/params.json", encoding="utf-8") as _handle:
    _params = json.load(_handle)
# Ordered: params.json is written in spec-input order (never sorted — see
# write_params), and dicts preserve insertion order, so INPUTS[0]/[1] land as
# the same left/right operands _SET_DIFFERENCE was given.
INPUTS = list(_params["inputs"].values())
TARGET = _params["output"]


def read_rows(name):
    with open("inputs/" + name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_rows(target, fields, rows):
    if target.endswith(".xlsx"):
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.title = "result"
        sheet.append(fields)
        for row in rows:
            sheet.append([row.get(field, "") for field in fields])
        book.save(target)
        return
    # Everything else is written as CSV text. The offline stand-in covers the
    # two demo shapes honestly rather than pretending to write Word.
    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})

'''

_SET_DIFFERENCE = '''
left = read_rows(INPUTS[0])
right = read_rows(INPUTS[1]) if len(INPUTS) > 1 else []
fields = list(left[0].keys()) if left else ["ticker"]
key = fields[0]
seen = {row.get(key) for row in right}
missing = [row for row in left if row.get(key) not in seen]
write_rows(TARGET, fields, missing)
print("%d of %d rows keyed on %s are missing from the second input"
      % (len(missing), len(left), key))
'''

_SUMMARY_STATS = '''
rows = read_rows(INPUTS[0])
summary = []
for field in list(rows[0].keys()) if rows else []:
    values = []
    for row in rows:
        try:
            values.append(float(row.get(field, "")))
        except (TypeError, ValueError):
            continue
    if not values:
        continue
    summary.append({
        "column": field,
        "count": str(len(values)),
        "min": "%.4f" % min(values),
        "max": "%.4f" % max(values),
        "mean": "%.4f" % (sum(values) / len(values)),
    })
write_rows(TARGET, ["column", "count", "min", "max", "mean"], summary)
print("summarised %d numeric column(s) over %d row(s)" % (len(summary), len(rows)))
'''

_INVENTORY = '''
summary = []
for name in INPUTS:
    rows = read_rows(name)
    summary.append({
        "input": name,
        "rows": str(len(rows)),
        "columns": ", ".join(rows[0].keys()) if rows else "",
    })
write_rows(TARGET, ["input", "rows", "columns"], summary)
print("described %d input file(s)" % len(summary))
'''

_BODIES = {
    "set_difference": (_SET_DIFFERENCE, "rows in the first input whose key is absent from the second"),
    "summary_stats": (_SUMMARY_STATS, "count, min, max and mean of every numeric column"),
}


class HeuristicLLM:
    """Deterministic, offline stand-in for a model.

    Keeps the pipeline runnable with no API key (fresh-clone demo, CI). It is
    intentionally dumb: real quality comes from AnthropicLLM.
    """

    name = "heuristic"

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        if output_format is RelevanceVerdict:
            return self._relevance(user)
        if output_format is CrystallizerOutput:
            return self._crystallize(user)
        if output_format is TaskSpec:
            return self._interpret(user)
        if output_format is SynthesizedScript:
            return self._synthesize(user)
        if output_format is RegistryDecision:
            # Offline matching is fingerprint-only by design: a regex cannot
            # judge whether two phrasings mean the same operation, and guessing
            # here would hand a request to code proven for a different job.
            return RegistryDecision(workflow=None, confidence=0.0, reason="offline: no model match")
        if output_format is MemoryDecision:
            # Fingerprint-only offline, for the same reason as RegistryDecision.
            return MemoryDecision(memory_id=None, confidence=0.0, reason="offline: no model match")
        raise NotImplementedError(f"HeuristicLLM has no rule for {output_format.__name__}")

    def _relevance(self, user: str) -> RelevanceVerdict:
        text = ""
        for line in user.splitlines():
            if line.startswith("text: "):
                text = line[len("text: ") :]
        lowered = text.lower()
        if any(re.search(p, lowered) for p in _NOISE_PATTERNS):
            return RelevanceVerdict(relevant=False, topic="chatter", confidence=0.6)
        relevant = any(w in lowered for w in _REQUEST_WORDS)
        return RelevanceVerdict(
            relevant=relevant,
            topic="work-request" if relevant else "chatter",
            confidence=0.6,
        )

    def _crystallize(self, user: str) -> CrystallizerOutput:
        handled = _handled_message_ids(user)
        owned: list[str] = []
        title = "Untitled request"
        for line in user.splitlines():
            m = _MESSAGE_LINE.match(line)
            if not m:
                continue
            # Messages belonging to an already-handled candidate are retired: the
            # prompt says not to report them again, and re-reporting them is what
            # made every later request in a conversation vanish.
            if m.group("id") in handled:
                continue
            text = m.group("text")
            lowered = text.lower()
            if any(re.search(p, lowered) for p in _NOISE_PATTERNS):
                continue
            if not any(w in lowered for w in _REQUEST_WORDS):
                continue
            owned.append(m.group("id"))
            if title == "Untitled request":
                title = text[:80]
        if not owned:
            return CrystallizerOutput(candidates=[])
        return CrystallizerOutput(
            candidates=[
                CandidateDraft(
                    # Keyed off the first message the candidate owns: stable while
                    # the same request keeps accumulating follow-ups, and different
                    # once a genuinely new request starts. A constant key meant a
                    # conversation could only ever produce one task.
                    candidate_key=f"heuristic-{owned[0]}",
                    title=title,
                    summary=title,
                    message_ids=owned,
                    state="ready",
                    missing_fields=[],
                    open_question=None,
                )
            ]
        )

    def _interpret(self, user: str) -> TaskSpec:
        ids: list[str] = []
        texts: list[str] = []
        for line in user.splitlines():
            m = _MESSAGE_LINE.match(line)
            if not m:
                continue
            ids.append(m.group("id"))
            texts.append(m.group("text"))
        blob = " ".join(texts).lower()

        operation = _first_match(_OPERATIONS, blob, default="synthesize")
        output_format = _first_match(_FORMATS, blob, default="")
        inputs = _sources(blob)

        recipient = None
        match = _RECIPIENT.search(blob)
        if match:
            recipient = match.group("who")
        elif "send me" in blob:
            recipient = "the requester"

        missing = []
        if not output_format:
            missing.append("output_format")
        if not inputs:
            missing.append("inputs")

        return TaskSpec(
            intent=texts[0] if texts else "unknown request",
            inputs=inputs,
            operation=operation,
            output_format=output_format or "unknown",
            recipient=recipient,
            urgency="high" if any(w in blob for w in _URGENT_WORDS) else "normal",
            missing_fields=missing,
            source_message_ids=ids,
            certainty=_HEURISTIC_CERTAINTY,
        )

    def _synthesize(self, user: str) -> SynthesizedScript:
        """Canned, not generated. See the README: offline synthesis is a lookup.

        These two scripts are the honest precursor to the Phase 4 registry —
        the same idea (a proven script for a known shape), chosen by keyword
        instead of by a matcher.
        """
        operation_match = _SYNTH_OPERATION.search(user)
        target_match = _SYNTH_TARGET.search(user)
        operation = operation_match.group("operation").strip() if operation_match else ""
        target = target_match.group("target").strip() if target_match else "deliverable/output.txt"
        inputs = _SYNTH_INPUT.findall(user)

        body, approach = _BODIES.get(operation, (_INVENTORY, "a description of each input"))
        if not inputs:
            # set_difference with nothing to read would IndexError. Describing
            # an empty input set produces an empty table, which the validator
            # rejects for having no rows — a clear failure instead of a crash.
            body, approach = _INVENTORY, "a description of each input"

        reasoning = f"Offline canned script for {operation or 'an unrecognised operation'}: {approach}."
        substitution = ""
        substitution_target = ""
        if not target.endswith((".xlsx", ".csv")):
            # write_rows only genuinely produces .xlsx (openpyxl) or CSV text.
            # Writing CSV bytes under the requested name (e.g. .docx) would be
            # a file that lies about what it is, and the validator checks the
            # deliverable's suffix, not its content — so that lie would pass
            # silently. Substitute CSV honestly and say so, both in the
            # printed summary and in reasoning, instead of faking the format.
            requested = target
            target = "deliverable/output.csv"
            substitution_target = f"TARGET = {target!r}\n"
            substitution = (
                'print("offline stand-in cannot write %s; wrote CSV to %s instead")\n'
                % (requested, target)
            )
            reasoning += f" Offline mode cannot write {requested}; wrote CSV to {target} instead."

        # INPUTS and TARGET now come from inputs/params.json, set in _PREAMBLE.
        # The substitution below still overrides TARGET when the requested
        # format is one the offline stand-in cannot honestly write.
        source = _PREAMBLE + substitution_target + body + substitution
        return SynthesizedScript(reasoning=reasoning, source=source)


def _handled_message_ids(user: str) -> set[str]:
    """Message ids the prompt marks as belonging to already-handled candidates."""
    ids: set[str] = set()
    in_handled = False
    for line in user.splitlines():
        if line.startswith("## "):
            in_handled = line.startswith(HANDLED_HEADER)
            continue
        if not in_handled:
            continue
        m = _OWNS.search(line)
        if m:
            ids.update(part.strip() for part in m.group("ids").split(",") if part.strip())
    return ids


def _first_match(table, blob: str, *, default: str) -> str:
    for words, value in table:
        if any(word in blob for word in words):
            return value
    return default


def _sources(blob: str) -> list[str]:
    found: list[str] = []
    covered: set[str] = set()
    for phrase in _SOURCE_PHRASES:
        if phrase not in blob:
            continue
        words = set(phrase.split())
        if words <= covered:
            continue
        found.append(phrase)
        covered |= words
    return found
