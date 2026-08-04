"""Surfaces a Home Assistant Repair Issue - plus a matching event for anyone
who wants to react to it themselves, e.g. with a push-notification
automation - when a device's connection keeps failing specifically at the
pairing/authentication stage, as opposed to being unreachable (out of range,
powered off), which is a separate failure mode the caller is expected to
already surface some other way.

Pairing failures are the one case where only the user can actually fix it
(confirm the prompt on the physical unit), so it's worth pointing out
directly instead of leaving it to look like any other transient disconnect.
"""
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

EVENT_PAIRING_REQUIRED = "madoka_pairing_required"
EVENT_PAIRING_RESOLVED = "madoka_pairing_resolved"

_LEARN_MORE_URL = "https://github.com/isakLevinson/madoka#bluetooth-pairing"


def _issue_id(addr):
    return f"pairing_required_{addr}"


def report_pairing_required(hass, addr, name):
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(addr),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="pairing_required",
        translation_placeholders={"name": name},
        learn_more_url=_LEARN_MORE_URL,
    )
    hass.bus.async_fire(EVENT_PAIRING_REQUIRED, {"address": addr, "name": name})


def clear_pairing_required(hass, addr, name):
    ir.async_delete_issue(hass, DOMAIN, _issue_id(addr))
    hass.bus.async_fire(EVENT_PAIRING_RESOLVED, {"address": addr, "name": name})
