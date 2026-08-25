from enum import Enum


class AutonomyMode(str, Enum):
    """How much the system does before a human sees it (spec §5.7).

    SUGGEST and COPILOT behave identically in 0.3.0 — both park at the single
    approval gate. They diverge in 0.4.0, when the real executor has mid-run
    checkpoints for COPILOT to stop at.
    """

    SUGGEST = "suggest"
    COPILOT = "copilot"
    AUTO = "auto"
