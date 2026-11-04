"""Config flow for ENGIE Particuliers."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import voluptuous as vol

from .vendor.engie_particuliers.client import (
    EngieClient,
    factor_destination,
    factor_needs_challenge,
)
from .vendor.engie_particuliers.exceptions import (
    ApiError,
    AuthenticationError,
    LockedOutError,
    MfaRequiredError,
    PasswordExpiredError,
)
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class EngieConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._username = ""
        self._password = ""
        self._client: Any = None
        self._factors: list[dict[str, Any]] = []
        self._factor_id = ""
        self._state_token = ""
        self._reauth_entry: config_entries.ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EngieOptionsFlowHandler:
        return EngieOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(self._username.lower())
            if self._reauth_entry is None:
                self._abort_if_unique_id_configured()

            self._close_client()
            self._client = EngieClient(self._username, self._password)
            try:
                await self.hass.async_add_executor_job(self._client.login)
                return await self._async_finish()
            except MfaRequiredError as err:
                self._state_token = err.state_token
                self._factors = list(err.factors or [])
                if len(self._factors) > 1:
                    return await self.async_step_mfa_factor()
                if self._factors:
                    self._factor_id = str(self._factors[0].get("id") or "")
                    return await self.async_step_mfa_code()
                errors["base"] = "mfa_required"
            except LockedOutError:
                errors["base"] = "locked_out"
            except PasswordExpiredError:
                errors["base"] = "password_expired"
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except ApiError:
                errors["base"] = "cannot_connect"
            except OSError:
                _LOGGER.exception("Connection error during ENGIE login")
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during ENGIE login")
                errors["base"] = "unknown"

        schema = USER_SCHEMA
        if self._reauth_entry is not None:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=self._reauth_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_mfa_factor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        choices = {
            str(item.get("id") or ""): self._factor_label(item)
            for item in self._factors
            if item.get("id")
        }
        if user_input is not None:
            self._factor_id = str(user_input.get("mfa_factor") or "")
            return await self.async_step_mfa_code()
        return self.async_show_form(
            step_id="mfa_factor",
            data_schema=vol.Schema(
                {vol.Required("mfa_factor"): vol.In(choices)}
            ),
            errors=errors,
        )

    async def async_step_mfa_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            factor = self._selected_factor()
            if factor_needs_challenge(factor):
                try:
                    pending = await self.hass.async_add_executor_job(
                        lambda: self._client.challenge_mfa(
                            factor_id=self._factor_id,
                            state_token=self._state_token,
                        )
                    )
                    self._state_token = str(
                        pending.get("state_token") or self._state_token
                    )
                except AuthenticationError:
                    errors["base"] = "mfa_send_failed"
                    return self.async_show_form(
                        step_id="mfa_code",
                        data_schema=vol.Schema({vol.Required("mfa_code"): str}),
                        errors=errors,
                    )
            return self.async_show_form(
                step_id="mfa_code",
                data_schema=vol.Schema({vol.Required("mfa_code"): str}),
                description_placeholders={
                    "destination": factor_destination(self._selected_factor())
                    or self._factor_label(self._selected_factor())
                },
            )

        code = str(user_input.get("mfa_code") or "").strip()
        try:
            await self.hass.async_add_executor_job(
                lambda: self._client.login(
                    mfa_code=code,
                    mfa_factor_id=self._factor_id,
                    state_token=self._state_token,
                )
            )
            return await self._async_finish()
        except AuthenticationError:
            errors["base"] = "invalid_mfa"
        except ApiError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during ENGIE MFA")
            errors["base"] = "unknown"
        return self.async_show_form(
            step_id="mfa_code",
            data_schema=vol.Schema({vol.Required("mfa_code"): str}),
            errors=errors,
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_user()

    async def _async_finish(self) -> ConfigFlowResult:
        data = {
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
        }
        session = self._client.session.to_dict()
        self._close_client()
        self.hass.data.setdefault(DOMAIN, {})
        self.hass.data[DOMAIN][f"pending_session_{self._username.lower()}"] = {
            "session": session,
            "obtained_at": time.time(),
        }

        if self._reauth_entry is not None:
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data_updates=data,
            )
        return self.async_create_entry(title=self._username, data=data)

    def _selected_factor(self) -> dict[str, Any] | None:
        for item in self._factors:
            if str(item.get("id") or "") == self._factor_id:
                return item
        return self._factors[0] if self._factors else None

    def _factor_label(self, factor: dict[str, Any] | None) -> str:
        if not factor:
            return "MFA"
        kind = str(factor.get("factorType") or "otp").upper()
        dest = factor_destination(factor)
        return f"{kind} → {dest}" if dest else kind

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


class EngieOptionsFlowHandler(config_entries.OptionsFlow):
    def _meter_source_choices(self) -> dict[str, str]:
        """Get the current detected source catalog for this configuration.

        The dropdown contains only hashed source IDs and masked labels.
        """
        runtime = (
            self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id) or {}
        )
        coordinator = runtime.get("coordinator")
        snapshot = getattr(coordinator, "data", None)
        return {
            source.source_id: source.display_name
            for source in (getattr(snapshot, "meter_sources", None) or [])
        }

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = dict(user_input)
            gas_pce = str(options.pop("gas_pce", "") or "").strip()
            clear_gas_pce = bool(options.pop("clear_gas_pce", False))
            if gas_pce:
                options["gas_pce_sha256"] = hashlib.sha256(
                    gas_pce.encode("ascii")
                ).hexdigest()
            elif not clear_gas_pce:
                existing = self.config_entry.options.get("gas_pce_sha256")
                if existing:
                    options["gas_pce_sha256"] = existing
            # Preserve previous source choices when the catalog is not
            # currently accessible (e.g. during a reload).
            if "selected_meter_sources" not in options:
                options["selected_meter_sources"] = list(
                    self.config_entry.options.get("selected_meter_sources") or []
                )
            return self.async_create_entry(title="", data=options)
        current = self.config_entry.options
        choices = self._meter_source_choices()
        stored_selection = current.get("selected_meter_sources")
        current_selection = (
            list(choices) if stored_selection is None else list(stored_selection)
        )
        current_selection = [
            source_id for source_id in current_selection if source_id in choices
        ]
        schema: dict[Any, Any] = {
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=int(current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
            ): vol.All(vol.Coerce(int), vol.Range(min=300, max=86400)),
            vol.Optional("gas_pce"): vol.All(str, vol.Match(r"^[0-9]{14}$")),
            vol.Optional("clear_gas_pce", default=False): bool,
            vol.Optional(
                "meter_source_mode",
                default=str(current.get("meter_source_mode") or "all"),
            ): vol.In({"all": "All meters", "selected": "Selected meters"}),
        }
        if choices:
            schema[vol.Optional(
                "selected_meter_sources", default=current_selection
            )] = cv.multi_select(choices)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
        )
