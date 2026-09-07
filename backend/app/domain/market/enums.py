"""SPEC-01 §1: market enums."""

from __future__ import annotations

from enum import StrEnum


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"

    @property
    def seconds(self) -> int:
        return {
            Timeframe.M1: 60,
            Timeframe.M5: 5 * 60,
            Timeframe.M15: 15 * 60,
            Timeframe.M30: 30 * 60,
            Timeframe.H1: 60 * 60,
            Timeframe.H4: 4 * 60 * 60,
            Timeframe.D1: 24 * 60 * 60,
            Timeframe.W1: 7 * 24 * 60 * 60,
        }[self]


class AssetClass(StrEnum):
    FX = "FX"
    METAL = "METAL"
    INDEX = "INDEX"
    CRYPTO = "CRYPTO"
    ENERGY = "ENERGY"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Session(StrEnum):
    SYDNEY = "SYDNEY"
    TOKYO = "TOKYO"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    LONDON_NY_OVERLAP = "LONDON_NY_OVERLAP"
    DEAD = "DEAD"


class Regime(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    COMPRESSION = "COMPRESSION"
    EXPANSION = "EXPANSION"
    RANGING = "RANGING"
    CHOPPY = "CHOPPY"
    UNKNOWN = "UNKNOWN"
