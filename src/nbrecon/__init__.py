"""nbrecon - NetBox server reconciliation.

Pipeline: collect -> correlate -> plan -> approve -> apply -> verify -> audit.

Every stage is read-only except ``apply``, which refuses to run without an
approved plan file.
"""

__version__ = "0.1.0"
