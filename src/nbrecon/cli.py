"""Command line interface.

The pipeline is deliberately split into separate commands, each writing an
artifact the next one reads:

    collect -> plan -> approve -> apply -> verify

Everything before ``apply`` is read-only. ``apply`` refuses to run without an
approval file whose digest matches the plan.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from . import __version__
from .apply.jira_apply import JiraApplier, JiraResult, lookup
from .apply.netbox_apply import NetBoxApplier
from .approve import interactive_approve
from .audit import RunArtifacts, digest, new_run_id, setup_logging
from .collect.jira import JiraClient
from .collect.netbox import NetBoxClient
from .collect.prometheus import PrometheusCollector, matches
from .collect.redfish import RedfishCollector
from .collect.ttsmi import AnsibleFacts, enrich
from .config import CustomFieldMap, Scope, Settings, SkuMap
from .correlate import correlate, probe_address
from .diff import DeviceContext, DiffEngine
from .errors import NbreconError, ScopeError
from .models import CollectionStatus, DevicePair, Plan, utcnow
from .ownership import OwnershipMatrix
from .report.render import render_console, render_markdown
from .runstore import RunStore
from .state import (
    dump_collection,
    dump_pairs,
    load_approval,
    load_collection,
    load_pairs,
    load_plan,
)
from .verify import verify_apply

console = Console()

COLLECTION_FILE = "collection.json"
PAIRS_FILE = "pairs.json"
PLAN_FILE = "plan.json"
REPORT_FILE = "report.md"
APPROVAL_FILE = "approval.json"
APPLY_FILE = "apply.json"
JIRA_FILE = "jira.json"
VERIFY_FILE = "verify.json"


class Context:
    def __init__(self, settings: Settings, verbose: bool) -> None:
        self.settings = settings
        self.verbose = verbose
        self.matrix = OwnershipMatrix.load(settings.config_dir)
        self.custom_fields = CustomFieldMap.load(settings.config_dir)
        self.sku_map = SkuMap.load(settings.config_dir)

    def artifacts(self, run_id: str) -> RunArtifacts:
        return RunArtifacts(root=self.settings.run.artifact_dir, run_id=run_id)

    def store(self) -> RunStore:
        return RunStore(self.settings.run.run_history_db)

    def netbox(self) -> NetBoxClient:
        return NetBoxClient(self.settings.netbox)

    def jira(self) -> JiraClient | None:
        return JiraClient(self.settings.jira) if self.settings.jira.configured else None


def _latest_run_id(root: Path) -> str | None:
    if not root.is_dir():
        return None
    runs = sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True)
    return runs[0] if runs else None


def _bmc_interface_name(matrix: OwnershipMatrix) -> str | None:
    """Which NetBox interface holds the BMC MAC, according to the matrix.

    Taken from the rule rather than hard-coded, so renaming the interface in
    config/ownership.yaml is enough to change what the snapshot reads.
    """
    rule = matrix.by_key("bmc_mac")
    if rule is None or not rule.enabled or rule.target_kind != "interface_mac":
        return None
    return rule.target_name or "bmc"


def _resolve_run(ctx: Context, run_id: str | None) -> RunArtifacts:
    resolved = run_id or _latest_run_id(ctx.settings.run.artifact_dir)
    if not resolved:
        raise click.ClickException(
            f"no runs found under {ctx.settings.run.artifact_dir}; run 'nbrecon collect' first"
        )
    artifacts = ctx.artifacts(resolved)
    if not artifacts.dir.is_dir():
        raise click.ClickException(f"run {resolved} not found in {ctx.settings.run.artifact_dir}")
    return artifacts


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="nbrecon")
@click.option("--env-file", type=click.Path(dir_okay=False), help="Path to .env (default: ./.env)")
@click.option("--config-dir", type=click.Path(file_okay=False), help="Override the config dir")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging")
@click.pass_context
def main(ctx: click.Context, env_file: str | None, config_dir: str | None, verbose: bool) -> None:
    """NetBox server reconciliation: dry-run first, approval gated, audited."""
    setup_logging(verbose=verbose)
    try:
        settings = Settings.load(env_file)
        if config_dir:
            settings.config_dir = Path(config_dir)
        ctx.obj = Context(settings, verbose)
    except NbreconError as exc:
        raise click.ClickException(str(exc)) from exc


# --- preflight ------------------------------------------------------------
@main.command()
@click.option("--show-custom-fields", is_flag=True, help="List NetBox custom fields on devices")
@click.pass_obj
def preflight(ctx: Context, show_custom_fields: bool) -> None:
    """Check connectivity and configuration before a real run.

    Use --show-custom-fields to see the custom fields that actually exist on
    dcim.device, then copy the names into config/netbox_fields.yaml. This tool
    never creates custom fields.
    """
    ok = True

    console.rule("Configuration")
    console.print(f"Config dir:   {ctx.settings.config_dir}")
    console.print(f"Artifacts:    {ctx.settings.run.artifact_dir}")
    console.print(f"Run history:  {ctx.settings.run.run_history_db}")
    console.print(f"Batch cap:    {ctx.settings.run.max_batch}")
    console.print(f"Ownership:    {len(ctx.matrix.rules)} fields, "
                  f"{len(ctx.matrix.writable_rules())} writable")
    if ctx.sku_map.is_empty:
        console.print("[yellow]SKU map is empty: every model will report as unmapped.[/yellow]")
    unmapped = ctx.custom_fields.unmapped()
    if unmapped:
        console.print(
            f"[yellow]Unmapped custom fields ({len(unmapped)}): {', '.join(unmapped)}"
            "\nThose fields are skipped entirely until mapped.[/yellow]"
        )

    console.rule("NetBox")
    try:
        client = ctx.netbox()
        status = client.check()
        console.print(f"[green]OK[/green] {ctx.settings.netbox.url} "
                      f"(NetBox {status.get('netbox-version', '?')})")
    except NbreconError as exc:
        ok = False
        console.print(f"[red]FAIL[/red] {exc}")
        client = None

    if show_custom_fields and client:
        console.rule("Custom fields on dcim.device")
        try:
            fields = client.custom_field_definitions()
        except NbreconError as exc:
            console.print(f"[red]could not list custom fields: {exc}[/red]")
        else:
            if not fields:
                console.print("[yellow]none found[/yellow]")
            for f in sorted(fields, key=lambda x: str(x.get("name"))):
                console.print(
                    f"  {f.get('name')}  [dim]type={f.get('type', {}).get('value', '?')} "
                    f"label={f.get('label') or '-'}[/dim]"
                )
            console.print(
                "\n[dim]Copy the relevant names into config/netbox_fields.yaml.[/dim]"
            )

    console.rule("Jira")
    if not ctx.settings.jira.configured:
        console.print("[yellow]not configured; IPT lookup and linking are disabled[/yellow]")
    else:
        try:
            who = ctx.jira().check()  # type: ignore[union-attr]
            console.print(f"[green]OK[/green] as {who.get('emailAddress') or who.get('accountId')}")
        except NbreconError as exc:
            ok = False
            console.print(f"[red]FAIL[/red] {exc}")
        if not ctx.settings.jira.can_write_issue_fields:
            console.print(
                "[yellow]SN / NetBox URL field IDs not set; backfill is disabled[/yellow]"
            )

    console.rule("Metrics")
    if not ctx.settings.metrics.configured:
        console.print("[yellow]not configured; node names are skipped[/yellow]")
    else:
        names = PrometheusCollector(ctx.settings.metrics).node_names()
        console.print(f"[green]OK[/green] {len(names)} node names")

    console.rule("BMC")
    if not ctx.settings.bmc.configured:
        ok = False
        console.print("[red]FAIL[/red] BMC credentials not set")
    else:
        console.print("[green]OK[/green] credentials present")
        if not ctx.settings.bmc.verify_tls:
            console.print("[yellow]BMC TLS verification disabled (lab only)[/yellow]")

    if not ok:
        sys.exit(1)


# --- collect --------------------------------------------------------------
@main.command()
@click.option("--scope-file", type=click.Path(exists=True, dir_okay=False), required=True)
@click.option("--ansible-facts", type=click.Path(exists=True), help="tt-smi export file or dir")
@click.option("--run-id", help="Reuse a specific run ID (default: generate one)")
@click.pass_obj
def collect(
    ctx: Context, scope_file: str, ansible_facts: str | None, run_id: str | None
) -> None:
    """Read NetBox, Redfish, tt-smi export, Jira and metrics. Read-only."""
    try:
        scope = Scope.from_file(Path(scope_file))
        scope.validate(ctx.settings.run.max_batch)
    except ScopeError as exc:
        raise click.ClickException(str(exc)) from exc

    run = run_id or new_run_id()
    artifacts = ctx.artifacts(run)
    artifacts.ensure()
    setup_logging(ctx.verbose, artifacts.path("run.log"))
    store = ctx.store()

    client = ctx.netbox()
    console.print(f"[bold]Run {run}[/bold]")
    console.print("Snapshotting NetBox...")
    devices = client.fetch_devices(scope)

    if scope.ansible_group and not ansible_facts:
        raise click.ClickException(
            "scope selects an ansible_group but --ansible-facts was not given; "
            "the tool reads exported facts and never connects over SSH"
        )

    export = AnsibleFacts.load(ansible_facts) if ansible_facts else AnsibleFacts.empty()
    if scope.ansible_group:
        allowed = {h.split(".", 1)[0].lower() for h in export.hosts_in_group(scope.ansible_group)}
        devices = [d for d in devices if (d.name or "").split(".", 1)[0].lower() in allowed]

    if len(devices) > scope.max_devices:
        raise click.ClickException(
            f"scope selected {len(devices)} devices, above max_devices={scope.max_devices}. "
            "Narrow the scope; small batches are deliberate during the pilot."
        )
    console.print(f"  {len(devices)} device(s) in scope")

    probe_ip_by_device: dict[int, str] = {}
    bmc_interfaces: dict[int, dict[str, Any]] = {}
    iface_name = _bmc_interface_name(ctx.matrix)
    for device in devices:
        address = probe_address(device)
        if address:
            probe_ip_by_device[device.id] = address
        if iface_name:
            existing = client.find_interface(device.id, iface_name)
            if existing:
                bmc_interfaces[device.id] = existing

    console.print(f"Querying Redfish on {len(probe_ip_by_device)} BMC(s)...")
    redfish = RedfishCollector(ctx.settings.bmc)
    host_by_ip = {}
    for device in devices:
        address = probe_ip_by_device.get(device.id)
        if not address:
            store.record_observation(
                run, device.name or str(device.id), CollectionStatus.FAILED, device.id,
                "no BMC IP in NetBox",
            )
            continue
        facts = redfish.collect(address)
        facts = enrich(facts, export, device.name or facts.hostname)
        host_by_ip[address] = facts
        store.record_observation(
            run, device.name or str(device.id), facts.status, device.id,
            "; ".join(facts.errors),
        )

    reachable = sum(1 for f in host_by_ip.values() if f.usable)
    console.print(f"  {reachable} reachable, {len(host_by_ip) - reachable} failed")

    nodes = PrometheusCollector(ctx.settings.metrics).node_names()

    payload = dump_collection(
        run, scope.as_dict(), ctx.settings.netbox.url, devices, host_by_ip,
        probe_ip_by_device, list(nodes), bmc_interfaces,
    )
    artifacts.write_json(COLLECTION_FILE, payload)
    store.record_run(run, "collect", str(scope.as_dict()), len(devices))
    console.print(f"[green]Collection written to {artifacts.path(COLLECTION_FILE)}[/green]")
    console.print(f"Next: [bold]nbrecon plan --run-id {run}[/bold]")


# --- plan -----------------------------------------------------------------
@main.command()
@click.option("--run-id", help="Run to plan (default: most recent)")
@click.option("--show-all", is_flag=True, help="Include report-only rows on the console")
@click.pass_obj
def plan(ctx: Context, run_id: str | None, show_all: bool) -> None:
    """Correlate and diff. Produces the dry-run plan. Writes nothing to NetBox."""
    artifacts = _resolve_run(ctx, run_id)
    payload = artifacts.read_json(COLLECTION_FILE)
    devices, host_by_ip, probes, nodes, bmc_interfaces = load_collection(payload)

    result = correlate(devices, host_by_ip, probes)
    console.print(
        f"Correlated {len(result.pairs)} pair(s); {len(result.unmatched)} unmatched, "
        f"{len(result.collection_failures)} collection failure(s)"
    )

    jira_client = ctx.jira()
    store = ctx.store()
    contexts: dict[int, DeviceContext] = {}

    for pair in result.pairs:
        pair.prometheus_node = matches(nodes, pair.netbox.name or (
            pair.host.hostname if pair.host else None
        ))
        if jira_client:
            serial = (pair.host.serial if pair.host else None) or pair.netbox.serial
            try:
                confirmed, candidates = lookup(
                    jira_client, serial, pair.netbox.name
                )
                pair.jira, pair.jira_candidates = confirmed, candidates
            except NbreconError as exc:
                console.print(f"[yellow]Jira lookup failed for {pair.netbox.name}: {exc}[/yellow]")
        contexts[pair.netbox.id] = DeviceContext(
            bmc_interface=bmc_interfaces.get(pair.netbox.id),
            unreachable_streak=store.consecutive_unreachable(
                pair.netbox.name or str(pair.netbox.id)
            ),
        )

    # Left unchecked at plan time so planning stays offline from the collection
    # artifact; apply re-checks the slug against NetBox before writing.
    known_slugs: set[str] | None = None
    engine = DiffEngine(
        ctx.matrix, ctx.custom_fields, ctx.sku_map, known_slugs, jira_enabled=bool(jira_client)
    )
    diff_result = engine.run(result.pairs, contexts)

    the_plan = Plan(
        run_id=artifacts.run_id,
        created_at=utcnow(),
        scope=payload.get("scope", {}),
        netbox_url=payload.get("netbox_url", ""),
        snapshot_last_updated={str(d.id): d.last_updated for d in devices},
        proposals=diff_result.proposals,
        unmatched=result.unmatched + diff_result.unmatched,
        collection_failures=result.collection_failures,
        stats={
            "devices_in_scope": len(devices),
            "pairs": len(result.pairs),
            "skipped_unmapped": diff_result.skipped_unmapped,
        },
    )

    artifacts.write_json(PAIRS_FILE, dump_pairs(result.pairs))
    artifacts.write_text(PLAN_FILE, the_plan.to_json())
    artifacts.write_text(REPORT_FILE, render_markdown(the_plan))
    store.record_run(artifacts.run_id, "plan", str(the_plan.scope), len(devices))

    render_console(the_plan, console, show_all=show_all)
    console.print(f"[green]Plan written to {artifacts.path(PLAN_FILE)}[/green]")
    if the_plan.writes:
        console.print(f"Next: [bold]nbrecon approve --run-id {artifacts.run_id}[/bold]")
    else:
        console.print("No changes. Nothing to approve.")


# --- approve --------------------------------------------------------------
@main.command()
@click.option("--run-id", help="Run to approve (default: most recent)")
@click.option("--approver", required=True, help="Who is approving this plan")
@click.pass_obj
def approve(ctx: Context, run_id: str | None, approver: str) -> None:
    """Review the plan interactively, per device and per field."""
    artifacts = _resolve_run(ctx, run_id)
    the_plan = load_plan(artifacts.read_json(PLAN_FILE))
    approved = interactive_approve(the_plan, approver, console)
    artifacts.write_text(APPROVAL_FILE, approved.to_json())
    console.print(f"[green]Approval written to {artifacts.path(APPROVAL_FILE)}[/green]")
    if any(d.approved for d in approved.decisions):
        console.print(f"Next: [bold]nbrecon apply --run-id {artifacts.run_id}[/bold]")


# --- apply ----------------------------------------------------------------
@main.command()
@click.option("--run-id", help="Run to apply (default: most recent)")
@click.option("--create-ipt", is_flag=True, help="Allow creating IPT tickets (off by default)")
@click.option("--no-journal", is_flag=True, help="Skip the NetBox journal entry")
@click.option("--yes", is_flag=True, help="Skip the final confirmation prompt")
@click.pass_obj
def apply(
    ctx: Context, run_id: str | None, create_ipt: bool, no_journal: bool, yes: bool
) -> None:
    """Write approved changes to NetBox, then link or create IPT tickets."""
    artifacts = _resolve_run(ctx, run_id)
    if not artifacts.exists(APPROVAL_FILE):
        raise click.ClickException(
            f"no approval file for run {artifacts.run_id}; run 'nbrecon approve' first. "
            "Apply never runs without a reviewed plan."
        )

    plan_text = artifacts.path(PLAN_FILE).read_text()
    the_plan = load_plan(artifacts.read_json(PLAN_FILE))
    approval = load_approval(artifacts.read_json(APPROVAL_FILE))

    approved_writes = [
        p for p in the_plan.writes if (p.device_id, p.field_key) in approval.approved_keys()
    ]
    if not approved_writes:
        console.print("[yellow]Nothing approved. Nothing to apply.[/yellow]")
        return

    device_count = len({p.device_id for p in approved_writes})
    console.print(
        f"[bold]About to write {len(approved_writes)} field(s) across {device_count} "
        f"device(s)[/bold] to {the_plan.netbox_url}"
    )
    console.print(f"Approved by {approval.approver} at {approval.approved_at.isoformat()}")
    if not yes and not click.confirm("Proceed?", default=False):
        console.print("Aborted.")
        return

    client = ctx.netbox()
    store = ctx.store()
    applier = NetBoxApplier(client, ctx.matrix, ctx.custom_fields, store)

    try:
        result = applier.apply(
            the_plan,
            approval,
            expected_digest=digest(plan_text),
            max_batch=ctx.settings.run.max_batch,
            journal=not no_journal,
        )
    except NbreconError as exc:
        raise click.ClickException(str(exc)) from exc

    artifacts.write_json(APPLY_FILE, result.as_dict())
    console.print(f"[green]Applied {result.applied_count} field(s)[/green]")
    for outcome in result.stale_devices:
        console.print(
            f"[yellow]Skipped {outcome.device_name or outcome.device_id}: edited since "
            "the snapshot; it will be re-diffed next run[/yellow]"
        )
    for outcome in result.failed_devices:
        console.print(f"[red]{outcome.device_name or outcome.device_id}: {outcome.error}[/red]")

    jira_result = _apply_jira(ctx, artifacts, the_plan, result, create_ipt)
    if jira_result:
        artifacts.write_json(JIRA_FILE, jira_result.as_dict())

    store.record_run(artifacts.run_id, "apply", str(the_plan.scope), device_count)
    console.print(f"Next: [bold]nbrecon verify --run-id {artifacts.run_id}[/bold]")


def _apply_jira(
    ctx: Context, artifacts: RunArtifacts, the_plan: Plan, result: Any, create_ipt: bool
) -> JiraResult | None:
    jira_client = ctx.jira()
    if not jira_client:
        console.print("[yellow]Jira not configured; skipping IPT linking[/yellow]")
        return None
    if not artifacts.exists(PAIRS_FILE):
        return None

    pairs: list[DevicePair] = load_pairs(artifacts.read_json(PAIRS_FILE))
    touched = {o.device_id for o in result.outcomes if o.applied}
    if not touched:
        return None

    if create_ipt:
        console.print("[yellow]--create-ipt is on: missing IPT tickets will be created[/yellow]")

    applier = JiraApplier(jira_client, create_enabled=create_ipt)
    jira_result = JiraResult()
    for pair in pairs:
        if pair.netbox.id not in touched:
            continue
        jira_result.outcomes.append(applier.process(pair, pair.netbox.url))

    for outcome in jira_result.outcomes:
        if outcome.error:
            console.print(f"[red]{outcome.device_name}: {outcome.error}[/red]")
        for flag in outcome.flags:
            console.print(f"[yellow]{outcome.device_name}: {flag}[/yellow]")
    if jira_result.created_count:
        console.print(f"[green]Created {jira_result.created_count} IPT ticket(s)[/green]")
    return jira_result


# --- verify ---------------------------------------------------------------
@main.command()
@click.option("--run-id", help="Run to verify (default: most recent)")
@click.pass_obj
def verify(ctx: Context, run_id: str | None) -> None:
    """Re-read NetBox and Jira and confirm the applied values are present."""
    artifacts = _resolve_run(ctx, run_id)
    if not artifacts.exists(APPLY_FILE):
        raise click.ClickException(f"run {artifacts.run_id} has no apply record to verify")

    jira_payload = artifacts.read_json(JIRA_FILE) if artifacts.exists(JIRA_FILE) else None
    result = verify_apply(
        ctx.netbox(),
        ctx.matrix,
        ctx.custom_fields,
        artifacts.read_json(APPLY_FILE),
        jira_payload,
        ctx.jira(),
    )
    artifacts.write_json(VERIFY_FILE, result.as_dict())

    if result.ok:
        console.print(f"[green]Verified {len(result.entries)} value(s); all match.[/green]")
        return
    console.print(f"[red]{len(result.failures)} value(s) did not verify:[/red]")
    for entry in result.failures:
        console.print(
            f"  device {entry.device_id} {entry.field_key}: expected {entry.expected!r}, "
            f"found {entry.observed!r} {entry.detail}"
        )
    sys.exit(1)


# --- history --------------------------------------------------------------
@main.command()
@click.option("--limit", default=20, show_default=True)
@click.option("--device", help="Show the unreachable streak for one device identifier")
@click.pass_obj
def history(ctx: Context, limit: int, device: str | None) -> None:
    """Inspect the local run history."""
    store = ctx.store()
    if device:
        streak = store.consecutive_unreachable(device)
        threshold = ctx.matrix.unreachable_runs_threshold
        console.print(f"{device}: {streak} consecutive unreachable run(s) (threshold {threshold})")
        if streak >= threshold:
            console.print("[yellow]Consider Offline. The tool never writes status.[/yellow]")
        return
    for run in store.recent_runs(limit):
        console.print(
            f"{run['run_id']}  {run['stage']:<8} devices={run['device_count']}  {run['scope']}"
        )


if __name__ == "__main__":  # pragma: no cover
    main()
