"""Render a plan for a human reviewer, on the console and as Markdown."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from rich.console import Console
from rich.table import Table

from ..models import Action, Plan, Proposal, UnmatchedEntry

ACTION_STYLE = {
    Action.WRITE: "bold yellow",
    Action.FLAG: "bold red",
    Action.SUGGEST: "cyan",
    Action.REPORT: "dim",
}

UNMATCHED_HEADINGS = {
    "in_netbox_not_on_host": "In NetBox, not found on host",
    "on_host_not_in_netbox": "On host, not in NetBox",
    "no_bmc_ip": "No BMC IP (Redfish cannot reach)",
    "missing_serial": "Missing serial - no automatic match, no writes",
    "duplicate_serial": "Duplicate serial - no automatic match, no writes",
    "collection_failed": "Collection failures (unknown, never cleared)",
    "missing_ipt": "Missing IPT ticket",
    "possible_ipt_match": "Possible IPT match - confirm before linking",
    "unmapped_sku": "Unmapped SKU - add to config/sku_map.yaml",
}


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if text.strip() else "(empty)"


def render_console(plan: Plan, console: Console | None = None, show_all: bool = False) -> None:
    console = console or Console()

    console.rule(f"nbrecon plan {plan.run_id}")
    console.print(f"NetBox: {plan.netbox_url}")
    console.print(f"Scope:  {plan.scope}")
    console.print()

    writes = plan.writes
    flags = [p for p in plan.proposals if p.action is Action.FLAG]
    suggestions = [p for p in plan.proposals if p.action is Action.SUGGEST]
    reports = [p for p in plan.proposals if p.action is Action.REPORT]

    summary = Table(title="Summary", show_header=True, header_style="bold")
    summary.add_column("Outcome")
    summary.add_column("Count", justify="right")
    summary.add_row("Proposed writes", str(len(writes)))
    summary.add_row("Devices with writes", str(len(plan.device_ids_with_writes)))
    summary.add_row("Flags for a human", str(len(flags)))
    summary.add_row("Suggestions", str(len(suggestions)))
    summary.add_row("Report-only entries", str(len(reports)))
    summary.add_row("Unmatched", str(len(plan.unmatched)))
    summary.add_row("Collection failures", str(len(plan.collection_failures)))
    console.print(summary)
    console.print()

    if writes:
        console.print("[bold]Proposed changes[/bold] (nothing is written until approved)")
        for device_id, items in _by_device(writes).items():
            name = items[0].device_name or f"device-{device_id}"
            table = Table(title=f"{name} (id {device_id})", show_header=True, header_style="bold")
            table.add_column("Field")
            table.add_column("Current")
            table.add_column("Proposed")
            table.add_column("Why")
            for p in items:
                label = p.field_label + (" *" if p.requires_explicit_approval else "")
                table.add_row(label, _fmt(p.current), _fmt(p.proposed), p.reason)
            console.print(table)
        console.print("[dim]* requires explicit per-field approval; cannot be bulk approved[/dim]")
        console.print()
    else:
        console.print("[green]No changes proposed.[/green]\n")

    _render_group(console, "Flagged for a human decision", flags)
    _render_group(console, "Suggestions", suggestions)
    if show_all:
        _render_group(console, "Report-only", reports)

    _render_unmatched(console, "Unmatched", plan.unmatched)
    _render_unmatched(console, "Collection failures", plan.collection_failures)

    skipped = plan.stats.get("skipped_unmapped") or {}
    if skipped:
        console.print("[bold]Skipped - no NetBox custom field mapped[/bold]")
        for key, reason in sorted(skipped.items()):
            console.print(f"  - {key}: {reason}")
        console.print("[dim]Run 'nbrecon preflight --show-custom-fields' and fill "
                      "config/netbox_fields.yaml.[/dim]\n")


def _render_group(console: Console, title: str, items: list[Proposal]) -> None:
    if not items:
        return
    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("Device")
    table.add_column("Field")
    table.add_column("NetBox")
    table.add_column("Host")
    table.add_column("Detail")
    for p in items:
        table.add_row(
            p.device_name or str(p.device_id),
            p.field_label,
            _fmt(p.current),
            _fmt(p.proposed),
            p.reason,
        )
    console.print(table)
    console.print()


def _render_unmatched(console: Console, title: str, entries: list[UnmatchedEntry]) -> None:
    if not entries:
        return
    console.print(f"[bold]{title}[/bold]")
    grouped: dict[str, list[UnmatchedEntry]] = defaultdict(list)
    for entry in entries:
        key = entry.reason.value if hasattr(entry.reason, "value") else str(entry.reason)
        grouped[key].append(entry)
    for reason, items in sorted(grouped.items()):
        console.print(f"  [bold]{UNMATCHED_HEADINGS.get(reason, reason)}[/bold] ({len(items)})")
        for entry in items:
            console.print(f"    - {entry.identifier}: {entry.detail}")
    console.print()


def _by_device(items: list[Proposal]) -> dict[int, list[Proposal]]:
    grouped: dict[int, list[Proposal]] = defaultdict(list)
    for p in items:
        grouped[p.device_id].append(p)
    return dict(sorted(grouped.items()))


def render_markdown(plan: Plan) -> str:
    """Markdown report, written to the run directory and attachable to a ticket."""
    lines: list[str] = [
        f"# NetBox reconciliation plan `{plan.run_id}`",
        "",
        f"- NetBox: {plan.netbox_url}",
        f"- Created: {plan.created_at.isoformat()}",
        f"- Scope: `{plan.scope}`",
        "",
        "## Summary",
        "",
        "| Outcome | Count |",
        "| --- | ---: |",
        f"| Proposed writes | {len(plan.writes)} |",
        f"| Devices with writes | {len(plan.device_ids_with_writes)} |",
        f"| Unmatched | {len(plan.unmatched)} |",
        f"| Collection failures | {len(plan.collection_failures)} |",
        "",
    ]

    writes = plan.writes
    if writes:
        lines += ["## Proposed changes", ""]
        for device_id, items in _by_device(writes).items():
            name = items[0].device_name or f"device-{device_id}"
            lines += [
                f"### {name} (id {device_id})",
                "",
                "| Field | Current | Proposed | Why |",
                "| --- | --- | --- | --- |",
            ]
            for p in items:
                label = p.field_label + (" \\*" if p.requires_explicit_approval else "")
                lines.append(f"| {label} | {_fmt(p.current)} | {_fmt(p.proposed)} | {p.reason} |")
            lines.append("")
    else:
        lines += ["## Proposed changes", "", "None.", ""]

    for title, action in (
        ("Flagged for a human decision", Action.FLAG),
        ("Suggestions", Action.SUGGEST),
        ("Report-only", Action.REPORT),
    ):
        items = [p for p in plan.proposals if p.action is action]
        if not items:
            continue
        lines += [
            f"## {title}",
            "",
            "| Device | Field | NetBox | Host | Detail |",
            "| --- | --- | --- | --- | --- |",
        ]
        for p in items:
            lines.append(
                f"| {p.device_name or p.device_id} | {p.field_label} | "
                f"{_fmt(p.current)} | {_fmt(p.proposed)} | {p.reason} |"
            )
        lines.append("")

    for title, entries in (
        ("Unmatched", plan.unmatched),
        ("Collection failures", plan.collection_failures),
    ):
        if not entries:
            continue
        lines += [f"## {title}", ""]
        grouped: dict[str, list[UnmatchedEntry]] = defaultdict(list)
        for entry in entries:
            key = entry.reason.value if hasattr(entry.reason, "value") else str(entry.reason)
            grouped[key].append(entry)
        for reason, items_ in sorted(grouped.items()):
            lines += [f"### {UNMATCHED_HEADINGS.get(reason, reason)}", ""]
            for entry in items_:
                lines.append(f"- `{entry.identifier}` - {entry.detail}")
            lines.append("")

    return "\n".join(lines)
