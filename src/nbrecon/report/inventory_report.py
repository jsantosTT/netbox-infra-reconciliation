"""Render the inventory audit for a human reviewer.

The report is organised around what the reader has to do next, not around the
shape of the data: what is safe to act on, what is blocked and why, and what
the spreadsheet itself needs fixed before any of it is trustworthy.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ..inventory import DuplicateGroup, InventoryRow, Issue, NetworkMap, ParseResult
from ..inventory_audit import (
    AMBIGUOUS,
    BY_HOSTNAME,
    BY_SERIAL,
    DIFFER,
    NETBOX_AGREES,
    NETBOX_EMPTY,
    SHEET_ERROR,
    UNDECIDABLE,
    UNMATCHED,
    AuditResult,
    DuplicateMember,
)

MATCH_LABEL = {
    BY_SERIAL: "matched on serial",
    BY_HOSTNAME: "matched on hostname only",
    AMBIGUOUS: "serial cannot identify one device",
    UNMATCHED: "no NetBox device",
}


def _fmt(value: str | None) -> str:
    return value if value else "-"


def _group_issues(issues: list[tuple[InventoryRow, Issue]]) -> list[tuple[str, list[str]]]:
    grouped: dict[str, list[str]] = {}
    for row, issue in issues:
        grouped.setdefault(f"{issue.column} {issue.kind}", []).append(row.hostname)
    return sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def render_console(
    parse: ParseResult,
    rows: list[InventoryRow],
    dup_hostnames: list[DuplicateGroup],
    dup_serials: list[DuplicateGroup],
    dup_bmc: list[DuplicateGroup],
    networks: NetworkMap,
    bad_networks: list[tuple[InventoryRow, str]],
    audit: AuditResult | None,
    console: Console | None = None,
) -> None:
    console = console or Console()

    console.rule("Inventory export")
    summary = Table(show_header=True, header_style="bold")
    summary.add_column("Input")
    summary.add_column("Count", justify="right")
    summary.add_row("Lines read", str(parse.total_lines))
    summary.add_row("Blank rows skipped", str(parse.skipped_blank))
    summary.add_row("Rows with a hostname", str(len(parse.rows)))
    summary.add_row("Rows in the selected statuses", str(len(rows)))
    summary.add_row("Rows with a serial", str(sum(1 for r in rows if r.serial)))
    summary.add_row("Rows with a BMC IP", str(sum(1 for r in rows if r.bmc_ip)))
    summary.add_row(
        "Rows with both", str(sum(1 for r in rows if r.serial and r.bmc_ip))
    )
    console.print(summary)

    if parse.normalised:
        console.print()
        for note, count in sorted(parse.normalised.items()):
            console.print(f"  [dim]{count} row(s): {note}[/dim]")
        console.print()

    issues = parse.issues
    if issues:
        console.rule("Cells that need a decision")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Rows", justify="right")
        table.add_column("Hosts")
        table.add_column("Problem")
        # Grouped by problem rather than listed per row: 21 hosts whose IP cell
        # says "DHCP" is one decision, not 21.
        for problem, hosts in _group_issues(issues):
            shown = ", ".join(hosts[:6])
            if len(hosts) > 6:
                shown += f", and {len(hosts) - 6} more"
            table.add_row(str(len(hosts)), shown, problem)
        console.print(table)
        console.print("[dim]Full per-row detail is in the Markdown report.[/dim]")

    live = [g for g in dup_bmc if not g.historical]
    historical = [g for g in dup_bmc if g.historical]
    if live:
        console.rule("[bold red]BMC addresses claimed by more than one live host")
        console.print(
            "Two hosts still in service cannot share one BMC address. Until this is "
            "resolved the tool would authenticate against one machine believing it is "
            "the other.\n"
        )
        for group in live:
            console.print(f"  [bold]{group.value}[/bold]  {group.describe()}")
        console.print()
    if historical:
        console.rule("BMC addresses reused after a rename")
        console.print(
            f"{len(historical)} address(es) appear on several rows where only one is still "
            "in service. That reads as a rename or re-rack with the old row kept, and is "
            "not acted on either way.\n"
        )

    if dup_hostnames:
        console.rule("Duplicate hostnames")
        for group in dup_hostnames:
            console.print(f"  [bold]{group.value}[/bold]  {group.describe()}")
        console.print()

    _render_duplicate_serials(console, dup_serials, audit)

    console.rule("BMC addresses outside a BMC network")
    if not networks.configured:
        console.print(
            "Not checked: no bmc_networks.yaml in the config dir. Copy "
            "config/bmc_networks.example.yaml to enable it.\n"
        )
    elif not bad_networks:
        console.print("All claimed BMC addresses fall inside a known BMC range.\n")
    else:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Host")
        table.add_column("BMC IP")
        table.add_column("Status")
        table.add_column("Where it really is")
        for row, where in bad_networks:
            table.add_row(row.hostname, _fmt(row.bmc_ip), row.status, where)
        console.print(table)

    if audit is None:
        console.rule("NetBox")
        console.print("Skipped: --offline.\n")
        return

    _render_netbox(console, audit)


def _render_duplicate_serials(
    console: Console,
    dup_serials: list[DuplicateGroup],
    audit: AuditResult | None,
) -> None:
    """Duplicated serials, split by what NetBox says each one means.

    The split is the point. "Two rows share a serial" sends someone to a
    datacenter; "two rows share a serial and NetBox already knows they differ"
    is a spreadsheet edit. They were previously the same finding.
    """
    live = [g for g in dup_serials if not g.historical]
    historical = [g for g in dup_serials if g.historical]

    if historical:
        console.rule("Serials reused after a replacement")
        console.print(
            f"{len(historical)} serial(s) appear on several rows where only one is still "
            "in service. That reads as a record kept after a swap, and is not acted on.\n"
        )

    if not live:
        return

    if audit is None:
        console.rule("Serials claimed by more than one live host")
        console.print("Not resolved: --offline, so NetBox was not consulted.\n")
        for group in live:
            console.print(f"  [bold]{group.value}[/bold]  {group.describe()}")
        console.print()
        return

    for verdict, heading, blurb in (
        (
            NETBOX_AGREES,
            "[bold red]Serial conflicts NetBox cannot settle",
            "NetBox carries the same duplicate, so neither source can say which machine "
            "is which. The serial has to be read from the hardware.",
        ),
        (
            SHEET_ERROR,
            "Spreadsheet errors NetBox already disproves",
            "NetBox holds a distinct serial for each of these hosts, so the duplicate is "
            "in the export alone. No hardware access needed; correct the sheet.",
        ),
        (
            UNDECIDABLE,
            "Duplicated serials that could not be resolved",
            "NetBox did not return a serial for enough of these hosts to compare.",
        ),
    ):
        found = [d for d in audit.duplicate_serials if d.verdict == verdict]
        if not found:
            continue
        console.rule(heading)
        console.print(blurb + "\n")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Sheet serial", overflow="fold")
        table.add_column("Host")
        table.add_column("Status")
        table.add_column("NetBox serial", overflow="fold")
        for res in found:
            for index, member in enumerate(res.members):
                table.add_row(
                    res.value if index == 0 else "",
                    member.hostname,
                    member.status,
                    _member_serial(member),
                )
        console.print(table)
        console.print()


def _member_serial(member: DuplicateMember) -> str:
    if member.lookup == "absent":
        return "[dim]no such device in NetBox[/dim]"
    if member.lookup == "several":
        return "[dim]several NetBox devices with this name[/dim]"
    return member.netbox_serial or "[dim]empty in NetBox[/dim]"


def _render_netbox(console: Console, audit: AuditResult) -> None:
    console.rule("Against NetBox")
    counts = Table(show_header=True, header_style="bold")
    counts.add_column("Outcome")
    counts.add_column("Rows", justify="right")
    for key in (BY_SERIAL, BY_HOSTNAME, AMBIGUOUS, UNMATCHED):
        counts.add_row(MATCH_LABEL[key], str(sum(1 for a in audit.audits if a.match == key)))
    counts.add_row("NetBox devices with no sheet row", str(len(audit.unmatched_devices)))
    console.print(counts)

    disagreements = [
        (a, c)
        for a in audit.audits
        for c in a.comparisons
        if c.verdict == DIFFER
    ]
    if disagreements:
        console.rule("The sheet and NetBox disagree")
        console.print(
            "Neither side is assumed correct. Nothing is proposed for these.\n"
        )
        table = Table(show_header=True, header_style="bold")
        table.add_column("Host")
        table.add_column("Field")
        table.add_column("Sheet")
        table.add_column("NetBox")
        for a, c in disagreements:
            table.add_row(a.row.hostname, c.label, _fmt(c.sheet), _fmt(c.netbox))
        console.print(table)

    gaps = [
        (a, c)
        for a in audit.audits
        for c in a.comparisons
        if c.verdict == NETBOX_EMPTY
    ]
    console.rule("What the sheet could contribute")
    if not gaps:
        console.print("Nothing: NetBox already holds a value everywhere the sheet does.\n")
    else:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Host")
        table.add_column("Field")
        table.add_column("Sheet value")
        table.add_column("Match")
        for a, c in gaps:
            table.add_row(a.row.hostname, c.label, _fmt(c.sheet), MATCH_LABEL[a.match])
        console.print(table)

    _render_seeds(console, audit)


def _render_seeds(console: Console, audit: AuditResult) -> None:
    console.rule("BMC addresses ready to seed")
    ready = audit.ready_seeds
    if ready:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Host")
        table.add_column("Serial")
        table.add_column("BMC IP")
        table.add_column("Network")
        table.add_column("Device")
        for seed in ready:
            table.add_row(
                seed.hostname, seed.serial, seed.address, seed.network, str(seed.device_id)
            )
        console.print(table)
        console.print(
            f"\n{len(ready)} address(es) matched on serial, inside a BMC network, with the "
            "NetBox field empty and an unassigned IPAM entry already present.\n"
        )
    else:
        console.print("None are ready to apply as things stand.\n")

    blocked = [a for a in audit.audits if a.blockers and a.row.bmc_ip]
    if not blocked:
        return
    console.rule("Blocked, with the reason")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Host")
    table.add_column("Why")
    for a in blocked:
        table.add_row(a.row.hostname, "; ".join(a.blockers))
    console.print(table)


def _markdown_duplicate_serials(audit: AuditResult) -> list[str]:
    if not audit.duplicate_serials:
        return []
    lines = [
        "## Serials claimed by more than one live host",
        "",
        "Each group was resolved by looking every host up in NetBox **by name**, since "
        "the serial is the field in dispute.",
        "",
        "| Sheet serial | Verdict | Host | Status | NetBox serial | What to do |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    order = {NETBOX_AGREES: 0, UNDECIDABLE: 1, SHEET_ERROR: 2}
    for res in sorted(audit.duplicate_serials, key=lambda r: (order.get(r.verdict, 9), r.value)):
        for index, member in enumerate(res.members):
            netbox = member.netbox_serial or f"_{member.lookup}_"
            lines.append(
                f"| {res.value if index == 0 else ''} "
                f"| {res.verdict if index == 0 else ''} "
                f"| {member.hostname} | {member.status} | {netbox} "
                f"| {res.action if index == 0 else ''} |"
            )
    lines.append("")
    return lines


def render_markdown(
    source: str,
    parse: ParseResult,
    rows: list[InventoryRow],
    dup_bmc: list[DuplicateGroup],
    bad_networks: list[tuple[InventoryRow, str]],
    audit: AuditResult | None,
) -> str:
    live = [g for g in dup_bmc if not g.historical]
    lines: list[str] = [
        "# Inventory audit",
        "",
        f"- Source: `{source}`",
        "- This report is read-only. Nothing was written to NetBox.",
        "",
        "## Input",
        "",
        "| Measure | Count |",
        "| --- | ---: |",
        f"| Lines read | {parse.total_lines} |",
        f"| Blank rows skipped | {parse.skipped_blank} |",
        f"| Rows with a hostname | {len(parse.rows)} |",
        f"| Rows in the selected statuses | {len(rows)} |",
        f"| Rows with a serial and a BMC IP | {sum(1 for r in rows if r.serial and r.bmc_ip)} |",
        f"| Cells that need a decision | {len(parse.issues)} |",
        "",
    ]

    if live:
        lines += [
            "## BMC addresses claimed by more than one live host",
            "",
            "Resolve these before seeding anything. Two hosts in service cannot share "
            "one BMC address.",
            "",
            "| BMC IP | Rows |",
            "| --- | --- |",
        ]
        lines += [f"| {g.value} | {g.describe()} |" for g in live]
        lines.append("")

    if bad_networks:
        lines += [
            "## BMC addresses outside a BMC network",
            "",
            "| Host | BMC IP | Status | Where it really is |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {r.hostname} | {_fmt(r.bmc_ip)} | {r.status} | {w} |" for r, w in bad_networks
        ]
        lines.append("")

    if parse.issues:
        lines += [
            "## Cells that need a decision",
            "",
            "| Line | Host | Problem |",
            "| --- | --- | --- |",
        ]
        lines += [f"| {r.line} | {r.hostname} | {i} |" for r, i in parse.issues]
        lines.append("")

    if audit is None:
        lines += ["## NetBox", "", "Skipped: run was `--offline`.", ""]
        return "\n".join(lines)

    lines += [
        "## Against NetBox",
        "",
        "| Outcome | Rows |",
        "| --- | ---: |",
    ]
    for key in (BY_SERIAL, BY_HOSTNAME, AMBIGUOUS, UNMATCHED):
        lines.append(
            f"| {MATCH_LABEL[key]} | {sum(1 for a in audit.audits if a.match == key)} |"
        )
    lines += [f"| NetBox devices with no sheet row | {len(audit.unmatched_devices)} |", ""]

    lines += _markdown_duplicate_serials(audit)

    disagreements = [
        (a, c) for a in audit.audits for c in a.comparisons if c.verdict == DIFFER
    ]
    if disagreements:
        lines += [
            "## The sheet and NetBox disagree",
            "",
            "Neither side is assumed correct; nothing is proposed for these.",
            "",
            "| Host | Field | Sheet | NetBox |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {a.row.hostname} | {c.label} | {_fmt(c.sheet)} | {_fmt(c.netbox)} |"
            for a, c in disagreements
        ]
        lines.append("")

    ready = audit.ready_seeds
    lines += ["## BMC addresses ready to seed", ""]
    if ready:
        lines += [
            "| Host | Serial | BMC IP | Network | NetBox device |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {s.hostname} | {s.serial} | {s.address} | {s.network} | {s.device_url} |"
            for s in ready
        ]
    else:
        lines.append("None are ready to apply as things stand.")
    lines.append("")

    blocked = [a for a in audit.audits if a.blockers and a.row.bmc_ip]
    if blocked:
        lines += [
            "## Blocked, with the reason",
            "",
            "| Host | Why |",
            "| --- | --- |",
        ]
        lines += [f"| {a.row.hostname} | {'; '.join(a.blockers)} |" for a in blocked]
        lines.append("")

    return "\n".join(lines)
