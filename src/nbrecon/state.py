"""Serialisation of pipeline artifacts.

Each stage writes a JSON artifact that the next stage reads, so a run can be
inspected, archived, and re-reviewed without re-querying any source system.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .models import (
    Action,
    ApprovedPlan,
    Category,
    CollectionStatus,
    Decision,
    DevicePair,
    HostFacts,
    JiraIssue,
    MatchMethod,
    NetBoxDevice,
    Plan,
    Proposal,
    TrayFacts,
    UnmatchedEntry,
    UnmatchedReason,
)


def dump_collection(
    run_id: str,
    scope: dict[str, Any],
    netbox_url: str,
    devices: list[NetBoxDevice],
    host_by_ip: dict[str, HostFacts],
    probe_ip_by_device: dict[int, str],
    prometheus_nodes: list[str],
    bmc_interfaces: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "scope": scope,
        "netbox_url": netbox_url,
        "devices": [asdict(d) for d in devices],
        "hosts": {ip: asdict(f) for ip, f in host_by_ip.items()},
        "probe_ip_by_device": {str(k): v for k, v in probe_ip_by_device.items()},
        "prometheus_nodes": sorted(prometheus_nodes),
        # Snapshotted here rather than re-read at plan time so the MAC already
        # on the interface is compared against, instead of looking empty.
        "bmc_interfaces": {str(k): v for k, v in (bmc_interfaces or {}).items()},
    }


def load_collection(payload: dict[str, Any]) -> tuple[
    list[NetBoxDevice],
    dict[str, HostFacts],
    dict[int, str],
    set[str],
    dict[int, dict[str, Any]],
]:
    devices = [NetBoxDevice(**d) for d in payload.get("devices", [])]
    hosts: dict[str, HostFacts] = {}
    for ip, raw in (payload.get("hosts") or {}).items():
        raw = dict(raw)
        raw["trays"] = [TrayFacts(**t) for t in raw.get("trays", [])]
        raw["status"] = CollectionStatus(raw.get("status", "ok"))
        hosts[ip] = HostFacts(**raw)
    probes = {int(k): v for k, v in (payload.get("probe_ip_by_device") or {}).items()}
    nodes = set(payload.get("prometheus_nodes") or [])
    interfaces = {
        int(k): v for k, v in (payload.get("bmc_interfaces") or {}).items() if v
    }
    return devices, hosts, probes, nodes, interfaces


def dump_pairs(pairs: list[DevicePair]) -> list[dict[str, Any]]:
    return [asdict(p) for p in pairs]


def load_pairs(raw: list[dict[str, Any]]) -> list[DevicePair]:
    pairs: list[DevicePair] = []
    for entry in raw:
        host_raw = entry.get("host")
        host = None
        if host_raw:
            host_raw = dict(host_raw)
            host_raw["trays"] = [TrayFacts(**t) for t in host_raw.get("trays", [])]
            host_raw["status"] = CollectionStatus(host_raw.get("status", "ok"))
            host = HostFacts(**host_raw)
        pairs.append(
            DevicePair(
                netbox=NetBoxDevice(**entry["netbox"]),
                host=host,
                match_method=MatchMethod(entry["match_method"]),
                match_evidence=entry.get("match_evidence", ""),
                jira=JiraIssue(**entry["jira"]) if entry.get("jira") else None,
                jira_candidates=[JiraIssue(**j) for j in entry.get("jira_candidates", [])],
                prometheus_node=entry.get("prometheus_node"),
            )
        )
    return pairs


def load_plan(payload: dict[str, Any]) -> Plan:
    plan = Plan(
        run_id=payload["run_id"],
        created_at=datetime.fromisoformat(payload["created_at"]),
        scope=payload.get("scope", {}),
        netbox_url=payload.get("netbox_url", ""),
        snapshot_last_updated=payload.get("snapshot_last_updated", {}),
        stats=payload.get("stats", {}),
    )
    plan.proposals = [
        Proposal(
            device_id=int(p["device_id"]),
            device_name=p.get("device_name"),
            field_key=p["field_key"],
            field_label=p.get("field_label", p["field_key"]),
            category=Category(p["category"]),
            action=Action(p["action"]),
            current=p.get("current"),
            proposed=p.get("proposed"),
            reason=p.get("reason", ""),
            target_kind=p.get("target_kind", "attribute"),
            target_name=p.get("target_name"),
            requires_explicit_approval=bool(p.get("requires_explicit_approval", False)),
        )
        for p in payload.get("proposals", [])
    ]
    plan.unmatched = [_unmatched(u) for u in payload.get("unmatched", [])]
    plan.collection_failures = [_unmatched(u) for u in payload.get("collection_failures", [])]
    plan.jira_actions = payload.get("jira_actions", [])
    return plan


def _unmatched(raw: dict[str, Any]) -> UnmatchedEntry:
    return UnmatchedEntry(
        reason=UnmatchedReason(raw["reason"]),
        identifier=raw["identifier"],
        detail=raw.get("detail", ""),
        device_id=raw.get("device_id"),
        netbox_url=raw.get("netbox_url"),
    )


def load_approval(payload: dict[str, Any]) -> ApprovedPlan:
    return ApprovedPlan(
        run_id=payload["run_id"],
        plan_digest=payload["plan_digest"],
        approver=payload["approver"],
        approved_at=datetime.fromisoformat(payload["approved_at"]),
        decisions=[
            Decision(
                device_id=int(d["device_id"]),
                field_key=d["field_key"],
                approved=bool(d["approved"]),
                note=d.get("note", ""),
            )
            for d in payload.get("decisions", [])
        ],
    )
