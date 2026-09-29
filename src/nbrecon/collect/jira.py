"""Jira IPT lookup and linking.

Search order per the ownership matrix: JQL on the SN custom field first, then a
text fallback on hostname or serial. A ticket is created only when both
searches come back empty *and* ``--create-ipt`` was passed.

TLS verification is always on.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..config import JiraSettings
from ..errors import ApplyError, CollectionError
from ..models import JiraIssue

LOG = logging.getLogger("nbrecon.jira")

TIMEOUT = 30


def _escape_jql(value: str) -> str:
    """Escape a literal for safe interpolation into a JQL string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class JiraClient:
    def __init__(self, settings: JiraSettings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.base = settings.url.rstrip("/") + "/"
        self.session = session or requests.Session()
        self.session.auth = (settings.user, settings.token)
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )

    # --- plumbing ---------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.base + path.lstrip("/")
        try:
            resp = self.session.post(url, json=payload, timeout=TIMEOUT, verify=True)
        except requests.RequestException as exc:
            raise CollectionError(f"Jira POST {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise CollectionError(f"Jira POST {url} returned {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _put(self, path: str, payload: dict[str, Any]) -> None:
        url = self.base + path.lstrip("/")
        try:
            resp = self.session.put(url, json=payload, timeout=TIMEOUT, verify=True)
        except requests.RequestException as exc:
            raise ApplyError(f"Jira PUT {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ApplyError(f"Jira PUT {url} returned {resp.status_code}: {resp.text[:300]}")

    def _search(self, jql: str) -> list[dict[str, Any]]:
        fields = ["summary", "key"]
        if self.settings.field_serial:
            fields.append(self.settings.field_serial)
        if self.settings.field_netbox_url:
            fields.append(self.settings.field_netbox_url)
        payload = self._post("rest/api/3/search", {"jql": jql, "maxResults": 20, "fields": fields})
        return payload.get("issues", [])

    def _issue_url(self, key: str) -> str:
        return f"{self.base}browse/{key}"

    def _to_issue(self, raw: dict[str, Any], method: str) -> JiraIssue:
        fields = raw.get("fields") or {}
        key = raw["key"]
        return JiraIssue(
            key=key,
            url=self._issue_url(key),
            summary=fields.get("summary"),
            serial_field=_field_text(fields.get(self.settings.field_serial)),
            netbox_url_field=_field_text(fields.get(self.settings.field_netbox_url)),
            match_method=method,
        )

    # --- lookups ----------------------------------------------------------
    def check(self) -> dict[str, Any]:
        url = self.base + "rest/api/3/myself"
        try:
            resp = self.session.get(url, timeout=TIMEOUT, verify=True)
        except requests.RequestException as exc:
            raise CollectionError(f"Jira connectivity check failed: {exc}") from exc
        if resp.status_code >= 400:
            raise CollectionError(f"Jira check returned {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def find_by_serial(self, serial: str) -> list[JiraIssue]:
        """Primary search: exact match on the SN custom field."""
        if not self.settings.field_serial:
            return []
        jql = (
            f'project = "{_escape_jql(self.settings.project_key)}" '
            f'AND "{_escape_jql(self.settings.field_serial)}" ~ "{_escape_jql(serial)}"'
        )
        return [self._to_issue(i, "jql_serial") for i in self._search(jql)]

    def find_by_text(self, serial: str | None, hostname: str | None) -> list[JiraIssue]:
        """Fallback search: serial or hostname appearing in issue text."""
        terms = [t for t in (serial, hostname) if t]
        if not terms:
            return []
        clauses = " OR ".join(f'text ~ "{_escape_jql(t)}"' for t in terms)
        jql = f'project = "{_escape_jql(self.settings.project_key)}" AND ({clauses})'
        return [self._to_issue(i, "text_fallback") for i in self._search(jql)]

    def get_issue(self, key: str) -> JiraIssue | None:
        url = self.base + f"rest/api/3/issue/{key}"
        try:
            resp = self.session.get(url, timeout=TIMEOUT, verify=True)
        except requests.RequestException as exc:
            raise CollectionError(f"Jira GET {url} failed: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise CollectionError(f"Jira GET {url} returned {resp.status_code}")
        return self._to_issue(resp.json(), "direct")

    # --- writes -----------------------------------------------------------
    def set_issue_fields(self, key: str, serial: str | None, netbox_url: str | None) -> list[str]:
        """Backfill the SN and NetBox URL fields. Empty targets only.

        Returns the field IDs actually written. Callers are responsible for
        having checked that the existing values are empty; this method does not
        overwrite because it is only ever handed blanks to fill.
        """
        payload: dict[str, Any] = {}
        if serial and self.settings.field_serial:
            payload[self.settings.field_serial] = serial
        if netbox_url and self.settings.field_netbox_url:
            payload[self.settings.field_netbox_url] = netbox_url
        if not payload:
            return []
        self._put(f"rest/api/3/issue/{key}", {"fields": payload})
        return sorted(payload)

    def create_issue(
        self, summary: str, description: str, serial: str | None, netbox_url: str | None
    ) -> JiraIssue:
        """Create an IPT ticket. Only ever called behind --create-ipt."""
        fields: dict[str, Any] = {
            "project": {"key": self.settings.project_key},
            "summary": summary,
            "issuetype": {"name": "Task"},
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": description}]}
                ],
            },
        }
        if serial and self.settings.field_serial:
            fields[self.settings.field_serial] = serial
        if netbox_url and self.settings.field_netbox_url:
            fields[self.settings.field_netbox_url] = netbox_url

        url = self.base + "rest/api/3/issue"
        try:
            resp = self.session.post(url, json={"fields": fields}, timeout=TIMEOUT, verify=True)
        except requests.RequestException as exc:
            raise ApplyError(f"Jira issue creation failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ApplyError(
                f"Jira issue creation returned {resp.status_code}: {resp.text[:400]}"
            )
        key = resp.json()["key"]
        return JiraIssue(
            key=key,
            url=self._issue_url(key),
            summary=summary,
            serial_field=serial,
            netbox_url_field=netbox_url,
            match_method="created",
        )


def _field_text(value: Any) -> str | None:
    """Flatten a Jira field value to text.

    Jira returns plain strings for text fields but objects for select lists and
    rich-text bodies, so the shape has to be probed rather than assumed.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("value", "name", "text"):
            if key in value:
                return str(value[key]).strip() or None
    return str(value).strip() or None
