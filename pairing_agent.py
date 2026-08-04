"""Automatic BLE pairing for Madoka devices.

BlueZ requires a pairing agent to be registered over D-Bus before it can
complete a pairing handshake - even "Just Works" pairing (no PIN) needs an
agent present to answer BlueZ's questions, or the pairing attempt just fails.
Neither bleak nor Home Assistant registers one by default.

This module registers a "DisplayYesNo" agent - the capability that tells
BlueZ this side can show a passkey and confirm it matches, so pairing
negotiates the device's normal Numeric Comparison method (the one it's
actually designed and tested against) rather than being forced down to
Just Works. Our RequestConfirmation() answers automatically instead of
actually prompting anyone - but note the device's own firmware independently
decides whether *it* also wants to show a code and wait for a physical
button press; that's not something this side's capability choice controls.
(An earlier version of this used "NoInputNoOutput"/Just Works instead, which
is more truly zero-touch but was observed to make at least one real device
disconnect/misbehave - see madoka.py's _connect() comment.)

Only supported on Linux (BlueZ) - bleak's own BlueZ backend already depends
on dbus-fast there, so this adds no new runtime dependency in practice.
"""
import logging
import sys

from .helpers import log_debug, log_info, log_warning, log_exception

logger = logging.getLogger(__name__)
logger.propagate = True

AGENT_PATH = "/org/madoka/agent"
AGENT_CAPABILITY = "DisplayYesNo"


async def register() -> object | None:
    """Register a Just Works pairing agent as BlueZ's default agent.

    Returns the open MessageBus connection (which the caller must keep a
    reference to for as long as the agent should stay registered - closing
    or garbage-collecting it drops the agent), or None if registration
    wasn't possible (non-Linux, no D-Bus, no BlueZ, etc.) - callers should
    treat that as non-fatal, since it just means pairing may need to be
    done manually instead of automatically.
    """
    if sys.platform != "linux":
        log_debug(logger, "pairing_agent: not on Linux, skipping BlueZ agent registration")
        return None

    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except ImportError:
        log_warning(logger, "pairing_agent: dbus-fast not available, skipping BlueZ agent registration")
        return None

    try:
        from ._pairing_agent_interface import PairingAgent

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        bus.export(AGENT_PATH, PairingAgent())

        introspection = await bus.introspect("org.bluez", "/org/bluez")
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        agent_manager = obj.get_interface("org.bluez.AgentManager1")

        await agent_manager.call_register_agent(AGENT_PATH, AGENT_CAPABILITY)
        await agent_manager.call_request_default_agent(AGENT_PATH)

        log_info(logger, f"pairing_agent: registered as default BlueZ pairing agent ({AGENT_CAPABILITY})")
        return bus

    except Exception:
        log_exception(logger, "pairing_agent: failed to register BlueZ pairing agent - pairing may need to be done manually")
        return None


async def unregister(bus) -> None:
    """Unregister the agent and close the D-Bus connection opened by register()."""
    if bus is None:
        return

    try:
        introspection = await bus.introspect("org.bluez", "/org/bluez")
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        agent_manager = obj.get_interface("org.bluez.AgentManager1")
        await agent_manager.call_unregister_agent(AGENT_PATH)
    except Exception:
        log_exception(logger, "pairing_agent: failed to unregister BlueZ pairing agent")

    try:
        bus.disconnect()
    except Exception:
        log_exception(logger, "pairing_agent: failed to close D-Bus connection")
