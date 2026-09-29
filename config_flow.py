import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import selector

from .const import DOMAIN, ALLOWED_MODES  # make sure DOMAIN = "madoka" in const.py

_LOGGER = logging.getLogger(__name__)

CONF_DEVICES = "devices"
CONF_MODES   = "modes"
CONF_PAIRING_TIMEOUT = "pairing_timeout"

def _unyaml_bool(m):
    # The frontend's YAML/object editor follows YAML 1.1, where unquoted
    # off/on/yes/no are booleans, not strings. Undo that so users don't have
    # to remember to quote "off" in the devices list.
    if m is False:
        return "off"
    if m is True:
        return "on"
    return m


def validate_devices(value) -> list:
    """Validate that the input is a list of {name, mac, [modes]} dicts."""
    if not isinstance(value, list):
        raise vol.Invalid("Must be a list of devices")

    for dev in value:
        if not isinstance(dev, dict) or "name" not in dev or "mac" not in dev:
            raise vol.Invalid("Each device must be a dict with 'name' and 'mac'")

        modes = dev.get(CONF_MODES)
        if modes is not None:
            if isinstance(modes, list):
                modes = [_unyaml_bool(m) for m in modes]
                dev[CONF_MODES] = modes

            if (not isinstance(modes, list) or "off" not in modes
                    or any(m not in ALLOWED_MODES for m in modes)):
                raise vol.Invalid(
                    f"'modes' for {dev.get('name')} must be a list containing 'off' "
                    f"and only values from {ALLOWED_MODES}"
                )

        pairing_timeout = dev.get(CONF_PAIRING_TIMEOUT)
        if pairing_timeout is not None:
            if isinstance(pairing_timeout, bool) or not isinstance(pairing_timeout, (int, float)) or pairing_timeout <= 0:
                raise vol.Invalid(
                    f"'pairing_timeout' for {dev.get('name')} must be a positive number of seconds"
                )
    return value


class MadokaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Madoka configuration flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                devices = validate_devices(user_input[CONF_DEVICES])
            except vol.Invalid as err:
                _LOGGER.warning("Invalid devices: %s", err)
                errors[CONF_DEVICES] = "invalid_devices"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_DEVICES: devices,
                    },
                )

        # Form schema (all serializable for HA UI)
        schema = vol.Schema({
            vol.Required(
                CONF_DEVICES,
                default=[
                    {"name": "Living Room", "mac": "AA:BB:CC:DD:EE:FF", "modes": ["off", "cool", "heat"]},
                    {"name": "Bedroom", "mac": "11:22:33:44:55:66", "modes": ["off", "auto"]},
                ],
            ): selector({"object": {}})
        })

        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )


    @staticmethod
    def async_get_options_flow(config_entry):
        return MadokaOptionsFlowHandler()


class MadokaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle updates to the integration options."""

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                devices = validate_devices(user_input[CONF_DEVICES])
            except vol.Invalid as err:
                _LOGGER.warning("Invalid devices: %s", err)
                errors[CONF_DEVICES] = "invalid_devices"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_DEVICES: devices,
                    },
                )

        current = self.config_entry.options or self.config_entry.data
        schema = vol.Schema({
            vol.Required(
                CONF_DEVICES,
                default=current.get(CONF_DEVICES, [])
            ): selector({"object": {}})
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

