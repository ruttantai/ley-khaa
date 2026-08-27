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
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .resolver import ResolvedInput

MANIFEST_NAME = "manifest.json"


def _validate_safe_filename(filename: str) -> None:
    r"""Reject filenames that could escape the workspace via path traversal.

    Raises ValueError if the filename is absolute, contains separators (/ or \),
    or is . or .. (current/parent directory references).
    """
    if not filename:
        raise ValueError("filename cannot be empty")
    if Path(filename).is_absolute():
        raise ValueError(f"filename cannot be absolute: {filename}")
    if "/" in filename or "\\" in filename:
        raise ValueError(f"filename cannot contain path separators: {filename}")
    if filename in (".", ".."):
        raise ValueError(f"filename cannot be a directory reference: {filename}")


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
            _validate_safe_filename(item.filename)
            (self.inputs_dir / item.filename).write_text(item.content, encoding="utf-8")

    def next_attempt_number(self) -> int:
        """The number a fresh attempt should take, continuing across rounds.

        Attempt files are never reused. A task re-executed after a clarification
        writes attempt_3.py and attempt_4.py rather than overwriting the first
        round's two — spec §5 says every failed attempt stays in generator/, and
        that has to hold across rounds, not just within one.
        """
        used = [
            int(match.group(1))
            for path in self.generator_dir.glob("attempt_*.py")
            if (match := re.fullmatch(r"attempt_(\d+)\.py", path.name))
        ]
        return max(used, default=0) + 1

    def write_generator(self, attempt: int, source: str) -> Path:
        path = self.generator_dir / f"attempt_{attempt}.py"
        path.write_text(source, encoding="utf-8")
        return path

    def write_run_script(self, attempt: int) -> Path:
        """The human-runnable re-entry point named in spec §5.11.

        Points at the attempt that actually succeeded, not at the last one
        written — a failed final attempt is kept for the audit trail but is not
        what re-running the bundle should execute.
        """
        path = self.generator_dir / "run.sh"
        path.write_text(
            "#!/bin/sh\n"
            "# Re-run the generator that produced this bundle's deliverable.\n"
            "# Run this from the bundle root — the directory holding inputs/.\n"
            f"exec python generator/attempt_{attempt}.py\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def deliverables(self) -> list[Path]:
        """Real files only — a symlink is excluded, never followed.

        `p.is_file()` alone follows links, and `os.symlink("/etc/hosts",
        "deliverable/output.csv")` is one line inside a synthesized script. Every
        consumer here reads through this: the validator would pass it, the
        manifest would stamp a sha256 over bytes the run never produced, and the
        bundle would attest a file it did not create.
        """
        return sorted(
            p for p in self.deliverable_dir.iterdir() if p.is_file() and not p.is_symlink()
        )

    def linked_deliverables(self) -> list[Path]:
        """Symlinks planted in deliverable/, so the validator can say what
        actually happened instead of reporting an empty directory."""
        return sorted(p for p in self.deliverable_dir.iterdir() if p.is_symlink())

    def clear_deliverables(self) -> None:
        """Empty deliverable/ before a fresh execution round.

        The workspace is idempotent and a task can be executed more than once —
        the escalate/answer/re-run loop sends it back through CLASSIFIED into the
        SAME bundle. Without this, a file from the previous round is still the
        alphabetically first deliverable, so the validator judges (and the
        manifest attests) a file this run never wrote. Called per RUN, not per
        attempt: attempt 2 legitimately supersedes attempt 1 inside one run.
        """
        for path in self.deliverable_dir.iterdir():
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)

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
