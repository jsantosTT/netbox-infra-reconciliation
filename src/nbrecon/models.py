"""Canonical data structures shared across the pipeline.

Collected values use ``None`` to mean "not determined". That is distinct from
an empty string, which means "the source positively reported nothing". Only the
latter is ever a real value; ``None`` can never produce a write.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


class Category(str, Enum):
    HOST_OWNED = "host_owned"
    TOOL_OWNED = "tool_owned"
    HUMAN_ONLY = "human_only"
    REPORT_ONLY = "report_only"

    @property
    def writable(self) -> bool:
        """Only host-owned and tool-owned fields have a write path."""
        return self in (Category.HOST_OWNED, Category.TOOL_OWNED)


class Policy(str, Enum):
    WRITE = "write"
    FILL_IF_EMPTY = "fill_if_empty"
    NEVER = "never"


class Action(str, Enum):
    """What the plan proposes for one field on one device."""

    WRITE = "write"
    FLAG = "flag"
    SUGGEST = "suggest"
    REPORT = "report"


class MatchMethod(str, Enum):
    SERIAL = "serial"
    HOSTNAME_SUGGESTED = "hostname_suggested"
    BMC_IP_SUGGESTED = "bmc_ip_suggested"
    UNMATCHED = "unmatched"


class CollectionStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    UNREACHABLE = "unreachable"


class UnmatchedReason(str, Enum):
    IN_NETBOX_NOT_ON_HOST = "in_netbox_not_on_host"
    ON_HOST_NOT_IN_NETBOX = "on_host_not_in_netbox"
    NO_BMC_IP = "no_bmc_ip"
    MISSING_SERIAL = "missing_serial"
    DUPLICATE_SERIAL = "duplicate_serial"
    COLLECTION_FAILED = "collection_failed"
    MISSING_IPT = "missing_ipt"
    POSSIBLE_IPT_MATCH = "possible_ipt_match"
    UNMAPPED_SKU = "unmapped_sku"


@dataclass
class TrayFacts:
    """One accelerator tray. Collected and reported only; never written in v1."""

    tray_id: str
    serial: str | None = None
    model: str | None = None
    part_number: str | None = None
    source: str = "redfish"


@dataclass
class HostFacts:
    """What the collectors read from a physical host."""

    bmc_ip: str | None = None
    hostname: str | None = None
    serial: str | None = None
    model: str | None = None
    part_number: str | None = None
    asset_tag: str | None = None
    bmc_ipv4: str | None = None
    bmc_mac: str | None = None
    power_state: str | None = None
    chassis_revision: str | None = None
    flash_version: str | None = None
    fw_version: str | None = None
    kmd_version: str | None = None
    smi_version: str | None = None
    topology: str | None = None
    trays: list[TrayFacts] = field(default_factory=list)
    status: CollectionStatus = CollectionStatus.OK
    sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Partial data is still diffable; only the known-nothing cases are not."""
        return self.status in (CollectionStatus.OK, CollectionStatus.PARTIAL)


@dataclass
class NetBoxDevice:
    """A NetBox device as captured in the snapshot."""

    id: int
    name: str | None
    serial: str | None
    asset_tag: str | None
    device_type_slug: str | None
    device_type_id: int | None
    site_slug: str | None
    status: str | None
    primary_ip4: str | None
    oob_ip: str | None
    url: str
    last_updated: str | None
    rack: str | None = None
    custom_fields: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass
class JiraIssue:
    key: str
    url: str
    summary: str | None = None
    serial_field: str | None = None
    netbox_url_field: str | None = None
    match_method: str = "jql_serial"


@dataclass
class DevicePair:
    """A NetBox device correlated with host facts."""

    netbox: NetBoxDevice
    host: HostFacts | None
    match_method: MatchMethod
    match_evidence: str = ""
    jira: JiraIssue | None = None
    jira_candidates: list[JiraIssue] = field(default_factory=list)
    prometheus_node: str | None = None

    @property
    def requires_explicit_approval(self) -> bool:
        """Suggestion-based matches can never be bulk-approved."""
        return self.match_method in (
            MatchMethod.HOSTNAME_SUGGESTED,
            MatchMethod.BMC_IP_SUGGESTED,
        )


@dataclass
class Proposal:
    """One proposed change, flag or suggestion for one field on one device."""

    device_id: int
    device_name: str | None
    field_key: str
    field_label: str
    category: Category
    action: Action
    current: Any = None
    proposed: Any = None
    reason: str = ""
    target_kind: str = "attribute"
    target_name: str | None = None
    requires_explicit_approval: bool = False

    @property
    def is_write(self) -> bool:
        return self.action is Action.WRITE


@dataclass
class UnmatchedEntry:
    reason: UnmatchedReason
    identifier: str
    detail: str = ""
    device_id: int | None = None
    netbox_url: str | None = None


@dataclass
class Plan:
    """The dry-run artifact reviewed before anything is written."""

    run_id: str
    created_at: datetime
    scope: dict[str, Any]
    netbox_url: str
    snapshot_last_updated: dict[str, str | None] = field(default_factory=dict)
    proposals: list[Proposal] = field(default_factory=list)
    unmatched: list[UnmatchedEntry] = field(default_factory=list)
    collection_failures: list[UnmatchedEntry] = field(default_factory=list)
    jira_actions: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def writes(self) -> list[Proposal]:
        return [p for p in self.proposals if p.is_write]

    @property
    def device_ids_with_writes(self) -> list[int]:
        seen: dict[int, None] = {}
        for p in self.writes:
            seen.setdefault(p.device_id, None)
        return list(seen)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=_isoformat, sort_keys=False)


@dataclass
class Decision:
    """A human's answer to one proposed write."""

    device_id: int
    field_key: str
    approved: bool
    note: str = ""


@dataclass
class ApprovedPlan:
    run_id: str
    plan_digest: str
    approver: str
    approved_at: datetime
    decisions: list[Decision] = field(default_factory=list)

    def approved_keys(self) -> set[tuple[int, str]]:
        return {(d.device_id, d.field_key) for d in self.decisions if d.approved}

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=_isoformat)
