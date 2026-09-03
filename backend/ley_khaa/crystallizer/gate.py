from datetime import datetime, timezone

from ..persistence.orm import CandidateRow, as_utc
from .candidate import CandidateState


class ReadinessGate:
    """Debounce: don't fire mid-thought (spec §5.3).

    A candidate emits only when the model called it ready, nothing is missing,
    no question is open, AND the conversation has gone quiet.
    """

    def __init__(self, debounce_seconds: int = 45) -> None:
        self.debounce_seconds = debounce_seconds

    def should_emit(
        self, row: CandidateRow, *, last_message_at: datetime | None, now: datetime
    ) -> bool:
        if row.state != CandidateState.READY.value:
            return False
        if row.missing_fields:
            return False
        if row.open_question:
            return False
        if last_message_at is None:
            # `MessageRepository.last_timestamp` answers None for a conversation
            # with no messages in it, and both callers hand its answer straight
            # here. "The conversation has gone quiet" is a claim about observed
            # traffic; with nothing observed there is no pause to have elapsed,
            # and promoting would mint a task whose own source messages are not
            # there. Withholding is the answer that cannot lose work.
            return False
        quiet_for = (now - as_utc(last_message_at)).total_seconds()
        return quiet_for >= self.debounce_seconds
