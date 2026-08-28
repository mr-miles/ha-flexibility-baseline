"""Octopus adapter: all network access.

The data object these calls produce is :class:`~..data.FlexibilityData`, which
lives at the package root because it is not Octopus's — :mod:`csv_source` builds
the same thing from files. It is re-exported here only because it is the return
type of :meth:`OctopusClient.async_account_data`.
"""

from __future__ import annotations

from ..data import FlexibilityData, MeterPoint, SessionResult
from .client import (
    KRAKEN_URL,
    OEBE_URL,
    REST_ROOT,
    AiohttpTransport,
    OctopusAuthError,
    OctopusClient,
    OctopusError,
    Transport,
    readings_from_intervals,
)

__all__ = [
    "AiohttpTransport",
    "FlexibilityData",
    "KRAKEN_URL",
    "MeterPoint",
    "OEBE_URL",
    "OctopusAuthError",
    "OctopusClient",
    "OctopusError",
    "REST_ROOT",
    "SessionResult",
    "Transport",
    "readings_from_intervals",
]
