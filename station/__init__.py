"""LAZEIMS offline station application (FastAPI + SQLite)."""

from __future__ import annotations

# Station software build version. Must be >= a package's software_min_version.
SOFTWARE_VERSION = "1.0.0"
# Sync/rules contract this build understands (must match the package).
SUPPORTED_RULES_VERSIONS = {"1.0"}
# Local SQLite schema version this build expects.
SCHEMA_VERSION = 3
