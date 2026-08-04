"""The BlueZ org.bluez.Agent1 D-Bus interface implementation itself, split
out from pairing_agent.py so that module can be imported unconditionally -
this one imports dbus_fast at module level, and is only ever imported lazily
after pairing_agent.register() has already confirmed dbus_fast is available.

Method names and signatures follow BlueZ's org.bluez.Agent1 interface
exactly (see bluez/doc/org.bluez.Agent.rst). For a "DisplayYesNo" agent,
BlueZ negotiates Numeric Comparison pairing and calls RequestConfirmation()
with the passkey to display and confirm - answered automatically here rather
than actually prompting anyone. RequestAuthorization/AuthorizeService are
answered automatically too. The PIN/passkey text-entry methods are
implemented as well (BlueZ requires the full interface to be present) but
reject, since there's no keyboard to actually satisfy those requests with.
"""
from dbus_fast import DBusError
from dbus_fast.annotations import DBusObjectPath, DBusStr, DBusUInt16, DBusUInt32
from dbus_fast.service import ServiceInterface, dbus_method

_NO_IO_ERROR = "org.bluez.Error.Rejected"


class PairingAgent(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @dbus_method()
    def Release(self) -> None:
        pass

    @dbus_method()
    def RequestPinCode(self, device: DBusObjectPath) -> DBusStr:
        raise DBusError(_NO_IO_ERROR, "agent has no input/output capability")

    @dbus_method()
    def DisplayPinCode(self, device: DBusObjectPath, pincode: DBusStr) -> None:
        pass

    @dbus_method()
    def RequestPasskey(self, device: DBusObjectPath) -> DBusUInt32:
        raise DBusError(_NO_IO_ERROR, "agent has no input/output capability")

    @dbus_method()
    def DisplayPasskey(self, device: DBusObjectPath, passkey: DBusUInt32, entered: DBusUInt16) -> None:
        pass

    @dbus_method()
    def RequestConfirmation(self, device: DBusObjectPath, passkey: DBusUInt32) -> None:
        pass  # empty reply = confirmed

    @dbus_method()
    def RequestAuthorization(self, device: DBusObjectPath) -> None:
        pass  # empty reply = authorized

    @dbus_method()
    def AuthorizeService(self, device: DBusObjectPath, uuid: DBusStr) -> None:
        pass  # empty reply = authorized

    @dbus_method()
    def Cancel(self) -> None:
        pass
