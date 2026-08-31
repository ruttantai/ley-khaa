import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..adapters.base import Destination
from ..orchestrator.orchestrator import IntakeResult, Orchestrator

logger = logging.getLogger(__name__)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "conversations"


class Simulator:
    """Replays a synthetic conversation through the real intake path.

    Timestamps are backdated so the readiness gate sees a settled conversation
    rather than one still in progress.

    Also a ChannelAdapter (spec §3.3): it already goes through IntakeGateway,
    so satisfying the protocol costs four members and means the interface has
    three implementations from the start rather than being shaped around Slack
    and bolted onto the others afterwards. It has no socket and no channel, so
    start/stop are no-ops and notify only logs — a fixture replay has nobody to
    answer.
    """

    name = "simulator"

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator

    async def start(self) -> None:
        """Nothing to connect. Replay is driven by POST /simulate, not by a socket."""

    async def stop(self) -> None:
        """Nothing to disconnect."""

    async def notify(self, dest: Destination, text: str) -> None:
        logger.info("simulator notification for %s: %s", dest.conversation_id, text)

    def available(self) -> list[str]:
        return sorted(p.stem for p in FIXTURES.glob("*.json"))

    def replay(self, name: str) -> list[IntakeResult]:
        path = FIXTURES / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"no conversation fixture named {name!r}")
        data = json.loads(path.read_text())

        messages = data["messages"]
        # Backdate: the last message lands 10 minutes ago.
        start = datetime.now(timezone.utc) - timedelta(minutes=10 + len(messages))
        results = []
        for i, m in enumerate(messages):
            results.append(
                self.orchestrator.ingest(
                    {
                        "source": "simulator",
                        "client": data.get("client", "demo"),
                        "conversation_id": data["conversation_id"],
                        "author": m["author"],
                        "text": m["text"],
                        "attachments": m.get("attachments", []),
                        "timestamp": (start + timedelta(minutes=i)).isoformat(),
                    },
                    # Ingesting one message at a time and letting each one run the
                    # gate would make every message look like the newest thing said
                    # long ago (all timestamps are backdated up front), which
                    # defeats the debounce gate that exists precisely to let a
                    # conversation settle before a candidate promotes. Promotion is
                    # terminal, so a request torn across two "settled" moments would
                    # end up as two half-specified tasks instead of one. Replay the
                    # whole conversation first with promotion skipped, then let a
                    # single sweep() judge the fully-formed candidates together.
                    promote=False,
                )
            )
        # Now that every message is in, the conversation really has gone quiet:
        # sweep once so the gate sees the true, final state of each candidate.
        swept_task_ids = self.orchestrator.sweep()
        if results:
            # Attribute the promoted tasks to the last message: the conversation
            # only settles once the last thing has been said.
            results[-1].task_ids.extend(swept_task_ids)
        return results
