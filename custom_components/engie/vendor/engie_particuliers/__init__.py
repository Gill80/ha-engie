"""Python client for ENGIE Particuliers (com.engie.particuliers v10.28.0)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .exceptions import (
    ApiError,
    AuthenticationError,
    EngieError,
    LockedOutError,
    MfaRequiredError,
    PasswordExpiredError,
)
from .models import (
    AccountBalance,
    AccountInfo,
    AccountSnapshot,
    Address,
    BillingSnapshot,
    BusinessPartner,
    CompteContrat,
    ConsumptionPoint,
    ContractPrice,
    ContractSnapshot,
    EnergyContract,
    Invoice,
    SessionInfo,
    TariffCadran,
)

if TYPE_CHECKING:
    from .client import EngieClient

__all__ = [
    "EngieClient",
    "EngieError",
    "AuthenticationError",
    "MfaRequiredError",
    "LockedOutError",
    "PasswordExpiredError",
    "ApiError",
    "SessionInfo",
    "AccountInfo",
    "BusinessPartner",
    "CompteContrat",
    "EnergyContract",
    "Address",
    "ContractPrice",
    "TariffCadran",
    "AccountSnapshot",
    "BillingSnapshot",
    "ContractSnapshot",
    "ConsumptionPoint",
    "Invoice",
    "AccountBalance",
]


def __getattr__(name: str):
    if name == "EngieClient":
        from .client import EngieClient as _Client

        return _Client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
