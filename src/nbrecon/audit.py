"""Run identity and the audit trail.

Three destinations, as agreed: filesystem artifacts, the local run-history
database, and a NetBox journal entry on each touched device.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import utcnow

LOG = logging.getLogger("nbrecon")


def new_run_id() -> str:
    """Sortable, collision-resistant, and readable in a NetBox changelog."""
    return f"{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def digest(payload: str) -> str:
    """Bind an approval to the exact plan it was given."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class RunArtifacts:
    """Per-run directory holding every artifact the pipeline produces."""

    root: Path
    run_id: str

    @property
    def dir(self) -> Path:
        return self.root / self.run_id

    def ensure(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir

    def path(self, name: str) -> Path:
        return self.dir / name

    def write_text(self, name: str, content: str) -> Path:
        self.ensure()
        target = self.path(name)
        target.write_text(content)
        # Artifacts can contain BMC addresses and serials; keep them owner-only.
        os.chmod(target, 0o600)
        LOG.debug("wrote artifact %s", target)
        return target

    def write_json(self, name: str, payload: Any) -> Path:
        return self.write_text(name, json.dumps(payload, indent=2, default=str))

    def read_json(self, name: str) -> Any:
        return json.loads(self.path(name).read_text())

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    LOG.setLevel(level)
    LOG.handlers.clear()

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    LOG.addHandler(stream)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        LOG.addHandler(file_handler)


def journal_comment(run_id: str, changes: list[tuple[str, Any, Any]]) -> str:
    """The NetBox journal entry body recorded on every applied device."""
    lines = [
        f"nbrecon reconciliation run {run_id}",
        "",
        "Applied changes:",
    ]
    for field_key, old, new in changes:
        lines.append(f"- {field_key}: {old!r} -> {new!r}")
    lines.append("")
    lines.append("Source: automated reconciliation against Redfish/tt-smi, approved by a human.")
    return "\n".join(lines)
