"""Node names from Prometheus.

v1 uses this for one thing only: does a node name exist in monitoring. Labels
are deliberately not collected. Reached either directly or through the Grafana
datasource proxy; TLS verification is always on.

A metrics outage must never affect a reconciliation run, so every failure here
degrades to an empty set rather than raising.
"""

from __future__ import annotations

import logging

import requests

from ..config import MetricsSettings

LOG = logging.getLogger("nbrecon.prometheus")

TIMEOUT = 30
DEFAULT_QUERY = "up"
NODE_LABELS = ("instance", "node", "nodename", "hostname")


class PrometheusCollector:
    def __init__(self, settings: MetricsSettings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        if settings.grafana_token and not settings.prometheus_url:
            self.session.headers.update({"Authorization": f"Bearer {settings.grafana_token}"})

    def node_names(self, query: str = DEFAULT_QUERY) -> set[str]:
        if not self.settings.configured:
            LOG.info("metrics not configured; skipping node name collection")
            return set()

        try:
            resp = self.session.get(
                self.settings.query_url,
                params={"query": query},
                timeout=TIMEOUT,
                verify=True,
            )
        except requests.RequestException as exc:
            LOG.warning("Prometheus query failed, continuing without node names: %s", exc)
            return set()

        if resp.status_code >= 400:
            LOG.warning("Prometheus query returned %s; continuing", resp.status_code)
            return set()

        try:
            payload = resp.json()
        except ValueError:
            LOG.warning("Prometheus returned non-JSON; continuing")
            return set()

        names: set[str] = set()
        for series in payload.get("data", {}).get("result", []):
            metric = series.get("metric") or {}
            for label in NODE_LABELS:
                value = metric.get(label)
                if value:
                    names.add(_strip_port(str(value)))
                    break
        LOG.info("collected %d node names from metrics", len(names))
        return names


def _strip_port(value: str) -> str:
    """``host:9100`` is the same node as ``host`` for name-existence purposes."""
    if ":" in value and not value.startswith("["):
        return value.rsplit(":", 1)[0]
    return value


def matches(node_names: set[str], hostname: str | None) -> str | None:
    """Return the monitoring node name for a host, comparing short names."""
    if not hostname or not node_names:
        return None
    if hostname in node_names:
        return hostname
    short = hostname.split(".", 1)[0].lower()
    for name in node_names:
        if name.split(".", 1)[0].lower() == short:
            return name
    return None
