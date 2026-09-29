"""The field ownership matrix, loaded from config/ownership.yaml.

The diff engine holds no per-field logic of its own. Everything it is allowed
to do comes from this matrix, so the rules can be reviewed as data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .models import Category, Policy

VALID_TARGET_KINDS = {"attribute", "custom_field", "oob_ip", "interface_mac", "none"}


@dataclass(frozen=True)
class FieldRule:
    key: str
    label: str
    category: Category
    policy: Policy
    target_kind: str
    target_name: str | None
    sources: tuple[str, ...] = ()
    on_conflict: str = "flag"
    mandatory: bool = False
    enabled: bool = True
    requires_explicit_approval: bool = False
    requires_mapping: str | None = None
    report_if_empty: bool = False
    suggest_rule: str | None = None
    notes: str = ""

    @property
    def writable(self) -> bool:
        """A field is writable only if its category, policy and target all allow it.

        All three must agree. A human-only field with ``policy: write`` is a
        configuration mistake and is rejected at load time, but this property
        stays defensive because it is the last gate before a PATCH is built.
        """
        return (
            self.enabled
            and self.category.writable
            and self.policy is not Policy.NEVER
            and self.target_kind != "none"
        )


@dataclass
class OwnershipMatrix:
    rules: list[FieldRule] = field(default_factory=list)
    unknown_never_clears: bool = True
    staleness_check: bool = True
    fill_if_empty_never_overwrites: bool = True
    unreachable_runs_threshold: int = 3

    @classmethod
    def load(cls, config_dir: Path) -> OwnershipMatrix:
        path = config_dir / "ownership.yaml"
        if not path.is_file():
            raise ConfigError(f"ownership matrix not found: {path}")
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

        raw_fields = data.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields:
            raise ConfigError(f"{path}: 'fields' must be a non-empty list")

        rules: list[FieldRule] = []
        seen: set[str] = set()
        for entry in raw_fields:
            rule = _parse_rule(entry, path)
            if rule.key in seen:
                raise ConfigError(f"{path}: duplicate field key {rule.key!r}")
            seen.add(rule.key)
            rules.append(rule)

        behaviour = data.get("rules") or {}
        matrix = cls(
            rules=rules,
            unknown_never_clears=bool(behaviour.get("unknown_never_clears", True)),
            staleness_check=bool(behaviour.get("staleness_check", True)),
            fill_if_empty_never_overwrites=bool(
                behaviour.get("fill_if_empty_never_overwrites", True)
            ),
            unreachable_runs_threshold=int(behaviour.get("unreachable_runs_threshold", 3)),
        )
        matrix.validate()
        return matrix

    def validate(self) -> None:
        """Reject a matrix that would let the tool write something it must not.

        These invariants are non-negotiable, so a config that contradicts them
        fails the run rather than being silently corrected.
        """
        if not self.unknown_never_clears:
            raise ConfigError(
                "ownership.yaml: unknown_never_clears cannot be disabled; a failed "
                "collection must never clear a NetBox field"
            )
        if not self.fill_if_empty_never_overwrites:
            raise ConfigError(
                "ownership.yaml: fill_if_empty_never_overwrites cannot be disabled"
            )
        for rule in self.rules:
            if not rule.category.writable and rule.policy is not Policy.NEVER:
                raise ConfigError(
                    f"ownership.yaml: field {rule.key!r} is {rule.category.value} so its "
                    f"policy must be 'never', found {rule.policy.value!r}"
                )
            if rule.policy is not Policy.NEVER and rule.target_kind == "none":
                raise ConfigError(
                    f"ownership.yaml: field {rule.key!r} has a write policy but no target"
                )

    def by_key(self, key: str) -> FieldRule | None:
        for rule in self.rules:
            if rule.key == key:
                return rule
        return None

    def writable_rules(self) -> list[FieldRule]:
        return [r for r in self.rules if r.writable]

    def reportable_rules(self) -> list[FieldRule]:
        return [r for r in self.rules if not r.writable]


def _parse_rule(entry: Any, path: Path) -> FieldRule:
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: every field entry must be a mapping")

    key = str(entry.get("key") or "").strip()
    if not key:
        raise ConfigError(f"{path}: a field entry is missing 'key'")

    try:
        category = Category(str(entry.get("category")).strip())
    except ValueError as exc:
        raise ConfigError(
            f"{path}: field {key!r} has unknown category {entry.get('category')!r}"
        ) from exc

    try:
        policy = Policy(str(entry.get("policy", "never")).strip())
    except ValueError as exc:
        raise ConfigError(
            f"{path}: field {key!r} has unknown policy {entry.get('policy')!r}"
        ) from exc

    target = entry.get("target") or {}
    if not isinstance(target, dict):
        raise ConfigError(f"{path}: field {key!r} has a malformed 'target'")
    target_kind = str(target.get("kind") or "none").strip()
    if target_kind not in VALID_TARGET_KINDS:
        raise ConfigError(
            f"{path}: field {key!r} has unknown target kind {target_kind!r}; "
            f"expected one of {sorted(VALID_TARGET_KINDS)}"
        )
    target_name = target.get("name")
    target_name = str(target_name).strip() if target_name else None

    return FieldRule(
        key=key,
        label=str(entry.get("label") or key),
        category=category,
        policy=policy,
        target_kind=target_kind,
        target_name=target_name,
        sources=tuple(str(s) for s in (entry.get("sources") or [])),
        on_conflict=str(entry.get("on_conflict") or "flag"),
        mandatory=bool(entry.get("mandatory", False)),
        enabled=bool(entry.get("enabled", True)),
        requires_explicit_approval=bool(entry.get("requires_explicit_approval", False)),
        requires_mapping=(
            str(entry["requires_mapping"]) if entry.get("requires_mapping") else None
        ),
        report_if_empty=bool(entry.get("report_if_empty", False)),
        suggest_rule=(str(entry["suggest_rule"]) if entry.get("suggest_rule") else None),
        notes=str(entry.get("notes") or "").strip(),
    )
