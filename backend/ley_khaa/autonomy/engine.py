from dataclasses import dataclass

from ..interpreter.spec import TaskSpec
from .modes import AutonomyMode

# --- confidence penalties -------------------------------------------------
_MISSING_FIELD_PENALTY = 0.2
_UNSETTLED_CONVERSATION_PENALTY = 0.1

# --- confidence bonuses ----------------------------------------------------
# Repetition is weak evidence, and it is capped so it stays weak: the whole
# bonus is smaller than one missing field's penalty, so a request that has been
# run twenty times still cannot act alone while it has a known gap.
_FAMILIARITY_BONUS = 0.05
_MAX_FAMILIARITY_BONUS = 0.15

# --- risk contributions ---------------------------------------------------
# Everything carries some risk; a request that only reads data carries little.
_BASELINE_RISK = 0.1
_DELIVERY_RISK = 0.35
_MONEY_RISK = 0.4
_URGENCY_RISK = 0.15

_MONEY_TERMS = (
    "invoice", "payment", "wire", "settle", "trade", "refund", "payroll", "billing", "$",
)
_DELIVERY_OPS = (
    "send", "email", "post", "deliver", "publish", "delete", "overwrite", "submit",
)

# --- thresholds -----------------------------------------------------------
# Auto is deliberately hard to earn: it is the only mode that acts without a human.
_AUTO_CONFIDENCE, _AUTO_RISK = 0.85, 0.25
_COPILOT_CONFIDENCE, _COPILOT_RISK = 0.6, 0.6

_VERB = {
    AutonomyMode.AUTO: "I suggest Auto",
    AutonomyMode.COPILOT: "I suggest Co-pilot",
    AutonomyMode.SUGGEST: "stay in Suggest",
}


@dataclass(frozen=True)
class Recommendation:
    mode: AutonomyMode
    confidence: float
    risk: float
    reason: str


def recommend(
    spec: TaskSpec,
    *,
    candidate_missing_fields: list[str] | None = None,
    familiarity: int = 0,
) -> Recommendation:
    """Score a spec and recommend a mode, with a reason a human can argue with.

    Pure and deterministic on purpose (§5.7): the dial is the feature a reader
    will poke at hardest, so its behaviour must be reproducible and its rules
    readable in one screen — not hidden inside a model call.
    """
    confidence, confidence_clauses = _confidence(
        spec, candidate_missing_fields or [], familiarity
    )
    risk, risk_clauses = _risk(spec)
    mode = _mode(confidence, risk)
    return Recommendation(
        mode=mode,
        confidence=confidence,
        risk=risk,
        reason=_reason(mode, confidence, risk, confidence_clauses + risk_clauses),
    )


def _confidence(
    spec: TaskSpec, candidate_missing: list[str], familiarity: int = 0
) -> tuple[float, list[str]]:
    clauses: list[str] = []
    score = spec.certainty
    if spec.missing_fields:
        score -= _MISSING_FIELD_PENALTY * len(spec.missing_fields)
        clauses.append(f"{len(spec.missing_fields)} field(s) still unknown")
    if candidate_missing:
        score -= _UNSETTLED_CONVERSATION_PENALTY
        clauses.append("the conversation never settled the details")
    # Repetition can only reward a request with no known gaps — neither a spec
    # field still missing nor a conversation that never settled the details.
    # Without this gate the bonus could lift either kind of gap back over the
    # AUTO threshold: the cap is smaller than each individual penalty, but that
    # is a bound between constants, not a guard on an absolute threshold — a
    # request docked to just below AUTO by either gap would get pushed back
    # over by repetition alone.
    if familiarity > 0 and not spec.missing_fields and not candidate_missing:
        score += min(_MAX_FAMILIARITY_BONUS, _FAMILIARITY_BONUS * familiarity)
        clauses.append(f"I've done this {familiarity} times before")
    return _clamp(score), clauses


def _risk(spec: TaskSpec) -> tuple[float, list[str]]:
    clauses: list[str] = []
    score = _BASELINE_RISK
    haystack = " ".join([spec.intent, spec.operation, spec.output_format, *spec.inputs]).lower()

    if spec.recipient or any(op in haystack for op in _DELIVERY_OPS):
        score += _DELIVERY_RISK
        clauses.append("it delivers something to someone")
    if any(term in haystack for term in _MONEY_TERMS):
        score += _MONEY_RISK
        clauses.append("it touches money")
    if spec.urgency == "high":
        score += _URGENCY_RISK
        clauses.append("it is marked urgent")
    return _clamp(score), clauses


def _mode(confidence: float, risk: float) -> AutonomyMode:
    if confidence >= _AUTO_CONFIDENCE and risk <= _AUTO_RISK:
        return AutonomyMode.AUTO
    if confidence >= _COPILOT_CONFIDENCE and risk <= _COPILOT_RISK:
        return AutonomyMode.COPILOT
    return AutonomyMode.SUGGEST


def _reason(mode: AutonomyMode, confidence: float, risk: float, clauses: list[str]) -> str:
    head = f"{confidence:.0%} sure, {_risk_label(risk)} risk"
    if clauses:
        head += " — " + ", ".join(clauses)
    return f"{head} → {_VERB[mode]}"


def _risk_label(risk: float) -> str:
    if risk <= _AUTO_RISK:
        return "low"
    if risk <= _COPILOT_RISK:
        return "medium"
    return "high"


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 4)
