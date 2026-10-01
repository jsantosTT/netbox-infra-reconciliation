"""Configuration loading: environment secrets plus the YAML rule files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .errors import ConfigError, ScopeError

DEFAULT_CONFIG_DIR = Path("config")
CONFIG_DIR_ENV = "NBRECON_CONFIG_DIR"

# TLS verification is non-negotiable for these. Only BMCs may opt out, and only
# for lab sites, because they ship self-signed certificates.
ALWAYS_VERIFY_TLS = ("netbox", "jira", "prometheus", "grafana")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass
class NetBoxSettings:
    url: str
    token: str
    verify_tls: bool = True  # never configurable

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)


@dataclass
class JiraSettings:
    url: str
    user: str
    token: str
    project_key: str = "IPT"
    field_serial: str = ""
    field_netbox_url: str = ""
    verify_tls: bool = True  # never configurable

    @property
    def configured(self) -> bool:
        return bool(self.url and self.user and self.token)

    @property
    def can_write_issue_fields(self) -> bool:
        """Without the custom field IDs we can link but not backfill."""
        return bool(self.field_serial or self.field_netbox_url)


@dataclass
class MetricsSettings:
    """Prometheus, reached directly or through the Grafana datasource proxy."""

    prometheus_url: str = ""
    grafana_url: str = ""
    grafana_token: str = ""
    grafana_datasource_uid: str = ""
    verify_tls: bool = True  # never configurable

    @property
    def configured(self) -> bool:
        if self.prometheus_url:
            return True
        return bool(self.grafana_url and self.grafana_token and self.grafana_datasource_uid)

    @property
    def query_url(self) -> str:
        if self.prometheus_url:
            return f"{self.prometheus_url.rstrip('/')}/api/v1/query"
        return (
            f"{self.grafana_url.rstrip('/')}"
            f"/api/datasources/proxy/uid/{self.grafana_datasource_uid}/api/v1/query"
        )


@dataclass
class BmcSettings:
    user: str
    password: str
    verify_tls: bool = True
    timeout: int = 20

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password)


@dataclass
class RunSettings:
    max_batch: int = 20
    artifact_dir: Path = field(default_factory=lambda: Path("var/runs"))
    run_history_db: Path = field(default_factory=lambda: Path("var/run-history.db"))


@dataclass
class Settings:
    netbox: NetBoxSettings
    jira: JiraSettings
    metrics: MetricsSettings
    bmc: BmcSettings
    run: RunSettings
    config_dir: Path

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> Settings:
        """Read .env (if present) then the environment.

        Values already exported in the environment win over the file, so a
        container can be driven entirely by ``--env-file`` or by real env vars.
        """
        if env_file:
            path = Path(env_file)
            if not path.is_file():
                raise ConfigError(f"env file not found: {path}")
            load_dotenv(path, override=False)
        else:
            default = Path(".env")
            if default.is_file():
                load_dotenv(default, override=False)

        return cls(
            netbox=NetBoxSettings(
                url=_env("NBRECON_NETBOX_URL"),
                token=_env("NBRECON_NETBOX_TOKEN"),
            ),
            jira=JiraSettings(
                url=_env("NBRECON_JIRA_URL"),
                user=_env("NBRECON_JIRA_USER"),
                token=_env("NBRECON_JIRA_TOKEN"),
                project_key=_env("NBRECON_JIRA_PROJECT_KEY", "IPT"),
                field_serial=_env("NBRECON_JIRA_FIELD_SERIAL"),
                field_netbox_url=_env("NBRECON_JIRA_FIELD_NETBOX_URL"),
            ),
            metrics=MetricsSettings(
                prometheus_url=_env("NBRECON_PROMETHEUS_URL"),
                grafana_url=_env("NBRECON_GRAFANA_URL"),
                grafana_token=_env("NBRECON_GRAFANA_TOKEN"),
                grafana_datasource_uid=_env("NBRECON_GRAFANA_DATASOURCE_UID"),
            ),
            bmc=BmcSettings(
                user=_env("NBRECON_BMC_USER"),
                password=_env("NBRECON_BMC_PASSWORD"),
                verify_tls=_env_bool("NBRECON_BMC_VERIFY_TLS", True),
                timeout=_env_int("NBRECON_BMC_TIMEOUT", 20),
            ),
            run=RunSettings(
                max_batch=_env_int("NBRECON_MAX_BATCH", 20),
                artifact_dir=Path(_env("NBRECON_ARTIFACT_DIR", "var/runs")),
                run_history_db=Path(_env("NBRECON_RUN_HISTORY_DB", "var/run-history.db")),
            ),
            config_dir=Path(_env(CONFIG_DIR_ENV, str(DEFAULT_CONFIG_DIR))),
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


@dataclass
class CustomFieldMap:
    """Logical field key -> real NetBox custom field name.

    A blank mapping is meaningful: it disables the field entirely rather than
    guessing a name or asking NetBox to create one.
    """

    mapping: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: Path) -> CustomFieldMap:
        data = _load_yaml(config_dir / "netbox_fields.yaml")
        raw = data.get("custom_fields") or {}
        if not isinstance(raw, dict):
            raise ConfigError("netbox_fields.yaml: 'custom_fields' must be a mapping")
        cleaned = {k: (v or "").strip() for k, v in raw.items()}
        return cls(mapping=cleaned)

    def resolve(self, logical_name: str) -> str | None:
        return (self.mapping.get(logical_name) or "").strip() or None

    def unmapped(self) -> list[str]:
        return sorted(k for k, v in self.mapping.items() if not v)


@dataclass
class SkuMap:
    """Redfish model / part number -> NetBox device type slug."""

    by_part_number: dict[str, str] = field(default_factory=dict)
    by_model: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: Path) -> SkuMap:
        data = _load_yaml(config_dir / "sku_map.yaml")

        def norm(raw: Any, label: str) -> dict[str, str]:
            raw = raw or {}
            if not isinstance(raw, dict):
                raise ConfigError(f"sku_map.yaml: '{label}' must be a mapping")
            return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if v}

        return cls(
            by_part_number=norm(data.get("by_part_number"), "by_part_number"),
            by_model=norm(data.get("by_model"), "by_model"),
        )

    def resolve(self, part_number: str | None, model: str | None) -> str | None:
        """Part number is checked first; it is the more specific identifier."""
        if part_number:
            hit = self.by_part_number.get(part_number.strip().lower())
            if hit:
                return hit
        if model:
            hit = self.by_model.get(model.strip().lower())
            if hit:
                return hit
        return None

    @property
    def is_empty(self) -> bool:
        return not self.by_part_number and not self.by_model


@dataclass
class Scope:
    """Which devices a run targets. An unscoped run is refused."""

    site: str = ""
    tenant: str = ""
    rack: str = ""
    tags: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    host_list: str = ""
    ansible_group: str = ""
    max_devices: int = 20

    @classmethod
    def from_file(cls, path: Path) -> Scope:
        data = _load_yaml(path)
        return cls(
            site=(data.get("site") or "").strip(),
            tenant=(data.get("tenant") or "").strip(),
            rack=(data.get("rack") or "").strip(),
            tags=[str(t).strip() for t in (data.get("tags") or []) if str(t).strip()],
            devices=[str(d).strip() for d in (data.get("devices") or []) if str(d).strip()],
            host_list=(data.get("host_list") or "").strip(),
            ansible_group=(data.get("ansible_group") or "").strip(),
            max_devices=int(data.get("max_devices") or 20),
        )

    def resolved_devices(self) -> list[str]:
        """Explicit device names, including any drawn from the host list file."""
        names = list(self.devices)
        if self.host_list:
            path = Path(self.host_list)
            if not path.is_file():
                raise ScopeError(f"host_list file not found: {path}")
            for line in path.read_text().splitlines():
                entry = line.split("#", 1)[0].strip()
                if entry:
                    names.append(entry)
        seen: dict[str, None] = {}
        for n in names:
            seen.setdefault(n, None)
        return list(seen)

    def require_selector(self) -> None:
        """At least one selector, for reads as well as writes.

        Separate from the batch cap because the two guard different things: an
        unscoped query is refused everywhere, while ``max_devices`` bounds how
        much a single run may *write* and so does not apply to a pure read.
        """
        if not any(
            [
                self.site,
                self.tenant,
                self.rack,
                self.tags,
                self.devices,
                self.host_list,
                self.ansible_group,
            ]
        ):
            raise ScopeError(
                "refusing to run without a scope: set at least one of "
                "site, tenant, rack, tags, devices, host_list or ansible_group"
            )

    def validate(self, max_batch: int) -> None:
        self.require_selector()
        if self.max_devices < 1:
            raise ScopeError("max_devices must be at least 1")
        if self.max_devices > max_batch:
            raise ScopeError(
                f"scope max_devices ({self.max_devices}) exceeds the configured "
                f"batch cap NBRECON_MAX_BATCH ({max_batch})"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "tenant": self.tenant,
            "rack": self.rack,
            "tags": self.tags,
            "devices": self.devices,
            "host_list": self.host_list,
            "ansible_group": self.ansible_group,
            "max_devices": self.max_devices,
        }
