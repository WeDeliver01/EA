"""Exceptions raised by domain code.

Lives inside ``domain`` (rather than ``core``) because domain imports nothing
else from the project (SPEC-00 §5) and these are raised from within it.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for errors raised by domain and engine code."""


class IllegalStateTransition(DomainError):
    def __init__(self, current: object, target: object) -> None:
        self.current = current
        self.target = target
        super().__init__(f"illegal transition: {current!r} -> {target!r}")


class InvalidStopDistance(DomainError):
    pass


class InvalidContractSpec(DomainError):
    pass
