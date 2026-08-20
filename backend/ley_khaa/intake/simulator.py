import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..orchestrator.orchestrator import IntakeResult, Orchestrator

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "conversations"


class Simulator:
    """Replays a synthetic conversation through the real intake path.

    Timestamps are backdated so the readiness gate sees a settled conversation
    rather than one still in progress.
    """

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator

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
                    }
                )
            )
        return results
