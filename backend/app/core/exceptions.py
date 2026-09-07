"""Application-wide exception types.

The domain-level errors live in ``app.domain.exceptions`` (domain imports
nothing from the project, so they can't live here); re-exported for
convenience so non-domain code has one place to import from.
"""

from __future__ import annotations

from app.domain.exceptions import (
    DomainError,
    IllegalStateTransition,
    InvalidContractSpec,
    InvalidStopDistance,
)

__all__ = [
    "DomainError",
    "IllegalStateTransition",
    "InvalidContractSpec",
    "InvalidStopDistance",
    "ConfigurationError",
]


class ConfigurationError(Exception):
    """Raised when application configuration is invalid or incomplete."""
