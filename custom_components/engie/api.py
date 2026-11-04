"""Async adapter around the sync ENGIE Particuliers client.

Login happens once. Access / refresh tokens are stored in Home Assistant
storage and reused across restarts. The coordinator refreshes the JWT
before it expires so price polls do not prompt for MFA again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from engie_particuliers.client import EngieClient
from engie_particuliers.exceptions import (
    ApiError,
    AuthenticationError,
    MfaRequiredError,
)
from engie_particuliers.models import AccountSnapshot, ConsumptionPoint, SessionInfo
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store

from .const import DEFAULT_EXPIRES_IN, DOMAIN, HISTORY_CHART_DAYS, TOKEN_REFRESH_SKEW

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


class EngieAPI:
    """Home Assistant facade for EngieClient."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._client = EngieClient(
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
        )
        self._store = Store(hass, STORAGE_VERSION, f"engie_{entry.entry_id}")
        self._obtained_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def username(self) -> str:
        return str(self._entry.data.get(CONF_USERNAME) or "")

    async def async_setup(self) -> None:
        pending_key = f"pending_session_{self.username.lower()}"
        pending = (self._hass.data.get(DOMAIN) or {}).pop(pending_key, None)
        if isinstance(pending, dict) and isinstance(pending.get("session"), dict):
            session = pending["session"]
            if session.get("access_token"):
                try:
                    await self._run(
                        self._client.restore_session,
                        session["access_token"],
                        session.get("refresh_token") or "",
                        id_token=session.get("id_token") or "",
                        expires_in=int(session.get("expires_in") or 0),
                        scope=session.get("scope") or "",
                    )
                    self._obtained_at = float(pending.get("obtained_at") or time.time())
                    await self._async_save_session(SessionInfo.from_dict(session))
                    _LOGGER.debug("Restored ENGIE session from config-flow login")
                    return
                except (AuthenticationError, ApiError) as err:
                    _LOGGER.warning(
                        "Config-flow ENGIE session rejected (%s); using stored session",
                        err,
                    )

        payload = await self._store.async_load() or {}
        session = payload.get("session") if isinstance(payload, dict) else None
        obtained = float(payload.get("obtained_at") or 0) if isinstance(payload, dict) else 0.0
        if isinstance(session, dict) and session.get("access_token"):
            try:
                await self._run(
                    self._client.restore_session,
                    session["access_token"],
                    session.get("refresh_token") or "",
                    id_token=session.get("id_token") or "",
                    expires_in=int(session.get("expires_in") or 0),
                    scope=session.get("scope") or "",
                )
                self._obtained_at = obtained or time.time()
                _LOGGER.debug("Restored ENGIE session from storage")
                return
            except (AuthenticationError, ApiError) as err:
                _LOGGER.warning("Stored ENGIE session rejected (%s); logging in again", err)
        await self.async_login()

    async def async_login(self) -> None:
        try:
            session = await self._run(self._client.login)
        except MfaRequiredError as err:
            raise ConfigEntryAuthFailed(
                "ENGIE requires MFA. Reconfigure the integration to enter the code."
            ) from err
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        self._obtained_at = time.time()
        await self._async_save_session(session)

    async def async_ensure_session(self) -> None:
        if self._needs_refresh():
            await self._async_refresh()

    @property
    def authenticated(self) -> bool:
        return bool(self._session_dict().get("access_token"))

    @property
    def gas_api_diagnostics(self) -> dict[str, list[dict[str, Any]]]:
        """Return redacted technical gas endpoint results when supported.

        Safe with the existing PyPI ha-engie-api, which lacks this feature.
        """
        return getattr(self._client, "gas_api_diagnostics", {})

    async def async_get_prices(self) -> list[Any]:
        snapshot = await self.async_get_snapshot()
        return [item.price for item in snapshot.contracts]

    async def async_get_snapshot(self) -> AccountSnapshot:
        await self.async_ensure_session()
        try:
            return await self._run(self._client.get_snapshot, daily_days=HISTORY_CHART_DAYS)
        except ApiError as err:
            if getattr(err, "status_code", 0) in {401, 403}:
                await self._async_refresh()
                return await self._run(
                    self._client.get_snapshot, daily_days=HISTORY_CHART_DAYS
                )
            raise

    async def async_get_consumption_points(
        self,
        contract_id: str,
        *,
        granularity: str = "day",
        days: int = 400,
    ) -> list[ConsumptionPoint]:
        await self.async_ensure_session()
        try:
            return await self._run(
                self._client.get_consumption_points_for_contract,
                contract_id,
                granularity=granularity,
                days=days,
            )
        except ApiError as err:
            if getattr(err, "status_code", 0) in {401, 403}:
                await self._async_refresh()
                return await self._run(
                    self._client.get_consumption_points_for_contract,
                    contract_id,
                    granularity=granularity,
                    days=days,
                )
            raise

    async def async_close(self) -> None:
        await self._run(self._client.close)

    def _needs_refresh(self) -> bool:
        session = self._session_dict()
        if not session.get("access_token"):
            return True
        expires_in = int(session.get("expires_in") or DEFAULT_EXPIRES_IN)
        if expires_in <= 0:
            expires_in = DEFAULT_EXPIRES_IN
        if not self._obtained_at:
            return True
        return time.time() >= self._obtained_at + expires_in - TOKEN_REFRESH_SKEW

    async def _async_refresh(self) -> None:
        try:
            session = await self._run(self._client.refresh_tokens)
            self._obtained_at = time.time()
            await self._async_save_session(session)
            _LOGGER.debug("Refreshed ENGIE access token")
            return
        except (AuthenticationError, ApiError) as err:
            _LOGGER.info("ENGIE token refresh failed (%s); logging in again", err)
        await self.async_login()

    async def _async_save_session(self, session: Any) -> None:
        payload = session.to_dict() if hasattr(session, "to_dict") else dict(session)
        await self._store.async_save(
            {"session": payload, "obtained_at": self._obtained_at}
        )

    def _session_dict(self) -> dict[str, Any]:
        try:
            return self._client.session.to_dict()
        except Exception:  # noqa: BLE001 — client raises if not logged in
            return {}

    async def _run(self, func, *args, **kwargs):
        async with self._lock:
            return await asyncio.to_thread(func, *args, **kwargs)
