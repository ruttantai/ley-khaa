"""The reproducible Output Bundle on disk (spec §5.11).

    task-<id>/
    ├── deliverable/   what the human asked for
    ├── generator/     the ACTUAL code that produced it, every attempt
    ├── inputs/        the exact bytes it ran against
    └── manifest.json  provenance

Every failed attempt stays in generator/. A bundle that hides its first failure
is not an audit trail.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .resolver import ResolvedInput

MANIFEST_NAME = "manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def create(cls, root: Path | str, task_id: str) -> Workspace:
        """Lay out (or re-open) the bundle for a task.

        Idempotent: the sweeper can re-enter a task, and re-creating the
        workspace must never wipe evidence from an earlier attempt.
        """
        workspace = cls(Path(root) / f"task-{task_id}")
        for directory in (workspace.inputs_dir, workspace.generator_dir, workspace.deliverable_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return workspace

    @property
    def inputs_dir(self) -> Path:
        return self.root / "inputs"

    @property
    def generator_dir(self) -> Path:
        return self.root / "generator"

    @property
    def deliverable_dir(self) -> Path:
        return self.root / "deliverable"

    def write_inputs(self, resolved: list[ResolvedInput]) -> None:
        for item in resolved:
            (self.inputs_dir / item.filename).write_text(item.content, encoding="utf-8")

    def write_generator(self, attempt: int, source: str) -> Path:
        path = self.generator_dir / f"attempt_{attempt}.py"
        path.write_text(source, encoding="utf-8")
        return path

    def deliverables(self) -> list[Path]:
        return sorted(p for p in self.deliverable_dir.iterdir() if p.is_file())

    def input_hashes(self) -> dict[str, str]:
        """filename -> sha256, so a script that rewrote its inputs is caught."""
        return {p.name: sha256_file(p) for p in sorted(self.inputs_dir.iterdir()) if p.is_file()}

    def write_manifest(self, manifest: dict) -> Path:
        path = self.root / MANIFEST_NAME
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def read_manifest(self) -> dict:
        path = self.root / MANIFEST_NAME
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
