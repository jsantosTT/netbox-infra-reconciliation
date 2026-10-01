"""Render a NetBox snapshot on the console."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ..models import NetBoxDevice
from ..snapshot import Summary


def _fmt(value: str | None) -> str:
    return value if value else "-"


def render_console(
    devices: list[NetBoxDevice],
    summary: Summary,
    netbox_url: str,
    scope: dict,
    console: Console | None = None,
    show_devices: bool = True,
) -> None:
    console = console or Console()

    console.rule("NetBox snapshot")
    console.print(f"NetBox: {netbox_url}")
    console.print(f"Scope:  {scope}")
    console.print()

    if show_devices and devices:
        table = Table(show_header=True, header_style="bold")
        # Fold rather than ellipsize: a truncated serial or address is worse
        # than useless, because it looks like a value you could act on.
        for heading in (
            "Name",
            "Serial",
            "Device type",
            "Status",
            "Rack",
            "OOB IP",
            "Primary IPv4",
        ):
            table.add_column(heading, overflow="fold")
        for device in devices:
            table.add_row(
                _fmt(device.name),
                _fmt(device.serial),
                _fmt(device.device_type_slug),
                _fmt(device.status),
                _fmt(device.rack),
                _fmt(device.oob_ip),
                _fmt(device.primary_ip4),
            )
        console.print(table)

    counts = Table(show_header=True, header_style="bold", title="Summary")
    counts.add_column("Measure")
    counts.add_column("Count", justify="right")
    counts.add_row("Devices in scope", str(summary.total))
    counts.add_row("With a serial", str(summary.with_serial))
    counts.add_row("Without a serial", str(summary.without_serial))
    counts.add_row("With an OOB IP", str(summary.with_oob_ip))
    counts.add_row("With a primary IPv4", str(summary.with_primary_ip4))
    counts.add_row("Reachable by Redfish", str(summary.probeable))
    counts.add_row("Not reachable", str(len(summary.unprobeable)))
    console.print(counts)

    for title, tally in (
        ("Status", summary.by_status),
        ("Device type", summary.by_device_type),
        ("Site", summary.by_site),
    ):
        if len(tally) <= 1 and "(none)" not in tally:
            continue
        breakdown = Table(show_header=True, header_style="bold", title=title)
        breakdown.add_column(title)
        breakdown.add_column("Devices", justify="right")
        for key, count in tally.items():
            breakdown.add_row(key, str(count))
        console.print(breakdown)

    if summary.without_serial:
        console.print(
            f"\n[yellow]{summary.without_serial} device(s) have no serial.[/yellow] "
            "Serial is the tool's only identity, so these cannot be matched "
            "automatically and nothing would ever be written to them."
        )

    if summary.duplicate_serials:
        console.print(
            f"\n[red]{len(summary.duplicate_serials)} serial(s) appear on more than one "
            f"device:[/red] {', '.join(summary.duplicate_serials)}. "
            "A duplicated serial means no automatic match and no writes."
        )

    if summary.unprobeable:
        shown = ", ".join(summary.unprobeable[:10])
        if len(summary.unprobeable) > 10:
            shown += f", and {len(summary.unprobeable) - 10} more"
        console.print(
            f"\n[yellow]{len(summary.unprobeable)} device(s) have neither an OOB IP nor a "
            f"primary IPv4:[/yellow] {shown}.\nRedfish is never attempted for these, so "
            "they are invisible to reconciliation until NetBox has an address."
        )
