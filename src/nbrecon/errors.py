"""Exception hierarchy.

Split so the CLI can distinguish an operator mistake (exit 2) from a genuine
failure against a remote system (exit 1).
"""


class NbreconError(Exception):
    """Base class for every error raised by this tool."""


class ConfigError(NbreconError):
    """Configuration is missing, malformed, or internally inconsistent."""


class ScopeError(ConfigError):
    """The run scope is absent or would select more devices than permitted."""


class CollectionError(NbreconError):
    """A source system could not be read."""


class CorrelationError(NbreconError):
    """Device identity could not be established."""


class ApprovalError(NbreconError):
    """A plan was not approved, or the approval file does not match the plan."""


class StaleSnapshotError(NbreconError):
    """NetBox changed after the snapshot was taken."""


class ApplyError(NbreconError):
    """A write to NetBox or Jira failed."""


class SafetyViolation(NbreconError):
    """An operation was blocked by a safety invariant.

    Raised only for conditions that should be impossible; it means a guard
    caught something the calling code should have prevented.
    """
