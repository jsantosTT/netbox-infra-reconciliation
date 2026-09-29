"""Interactive approval, per device and per field.

The approval artifact is bound to the plan by a SHA-256 digest, so a plan that
is regenerated or edited after approval cannot be applied with a stale
sign-off.
"""

from __future__ import annotations

from collections import defaultdict

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .audit import digest
from .models import ApprovedPlan, Decision, Plan, Proposal, utcnow


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if text.strip() else "(empty)"


def _group(proposals: list[Proposal]) -> dict[int, list[Proposal]]:
    grouped: dict[int, list[Proposal]] = defaultdict(list)
    for p in proposals:
        grouped[p.device_id].append(p)
    return dict(sorted(grouped.items()))


def interactive_approve(
    plan: Plan, approver: str, console: Console | None = None
) -> ApprovedPlan:
    """Walk the reviewer through every proposed write.

    Bulk approval is offered per device, but any field marked as requiring
    explicit approval is always asked individually, even inside a device the
    reviewer accepted wholesale.
    """
    console = console or Console()
    writes = plan.writes
    approved = ApprovedPlan(
        run_id=plan.run_id,
        plan_digest=digest(plan.to_json()),
        approver=approver,
        approved_at=utcnow(),
    )

    if not writes:
        console.print("[green]No proposed changes to approve.[/green]")
        return approved

    grouped = _group(writes)
    console.print(
        f"[bold]{len(writes)} proposed change(s) across {len(grouped)} device(s).[/bold]"
    )
    console.print("[dim]Nothing is written until this review completes.[/dim]\n")

    for device_id, proposals in grouped.items():
        name = proposals[0].device_name or f"device-{device_id}"
        table = Table(title=f"{name} (id {device_id})", show_header=True, header_style="bold")
        table.add_column("#", justify="right")
        table.add_column("Field")
        table.add_column("Current")
        table.add_column("Proposed")
        table.add_column("Why")
        for idx, p in enumerate(proposals, start=1):
            label = p.field_label + (" *" if p.requires_explicit_approval else "")
            table.add_row(str(idx), label, _fmt(p.current), _fmt(p.proposed), p.reason)
        console.print(table)

        bulk_eligible = [p for p in proposals if not p.requires_explicit_approval]
        explicit_only = [p for p in proposals if p.requires_explicit_approval]

        if explicit_only:
            console.print(
                "[yellow]* these fields need an individual decision and are excluded "
                "from 'approve all'.[/yellow]"
            )

        choice = Prompt.ask(
            "Approve",
            choices=["all", "none", "each", "quit"],
            default="each",
        )

        if choice == "quit":
            console.print("[yellow]Review stopped. Remaining devices are left undecided.[/yellow]")
            break

        if choice == "none":
            for p in proposals:
                approved.decisions.append(
                    Decision(device_id, p.field_key, False, "device rejected by reviewer")
                )
            continue

        if choice == "all":
            for p in bulk_eligible:
                approved.decisions.append(
                    Decision(device_id, p.field_key, True, "bulk approved for device")
                )
            for p in explicit_only:
                ok = Confirm.ask(
                    f"  [bold]{p.field_label}[/bold]: {_fmt(p.current)} -> {_fmt(p.proposed)}?",
                    default=False,
                )
                approved.decisions.append(
                    Decision(device_id, p.field_key, ok, "explicit decision required")
                )
            continue

        for p in proposals:
            ok = Confirm.ask(
                f"  [bold]{p.field_label}[/bold]: {_fmt(p.current)} -> {_fmt(p.proposed)}?",
                default=False,
            )
            note = "explicit decision required" if p.requires_explicit_approval else "per-field"
            approved.decisions.append(Decision(device_id, p.field_key, ok, note))

    yes = sum(1 for d in approved.decisions if d.approved)
    no = len(approved.decisions) - yes
    undecided = len(writes) - len(approved.decisions)
    console.print(
        f"\n[bold]Approved:[/bold] {yes}  [bold]Rejected:[/bold] {no}  "
        f"[bold]Undecided:[/bold] {undecided}"
    )
    return approved


def approve_from_decisions(
    plan: Plan, approver: str, approved_keys: set[tuple[int, str]]
) -> ApprovedPlan:
    """Build an approval non-interactively.

    Used by tests and by a future non-CLI approval path. Fields that require
    explicit approval must be named individually here too; they are never
    inferred from a device-wide grant.
    """
    approved = ApprovedPlan(
        run_id=plan.run_id,
        plan_digest=digest(plan.to_json()),
        approver=approver,
        approved_at=utcnow(),
    )
    for p in plan.writes:
        key = (p.device_id, p.field_key)
        approved.decisions.append(
            Decision(p.device_id, p.field_key, key in approved_keys, "non-interactive")
        )
    return approved
