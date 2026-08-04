# Madoka

A Home Assistant custom integration that talks directly to Daikin BRC1H
("Madoka") BLE wireless controllers over Bluetooth LE — no cloud, no Wi-Fi
bridge. Each controller is exposed as a `climate` entity.

## Features

- Power on/off, and (per unit) cool/heat mode selection
- Target temperature and fan speed (low/medium/high) control
- Filter-needs-cleaning exposed as an entity attribute
- Automatic BLE reconnect with connection/offline tracking
- Failed writes (e.g. a dropped BLE command) are kept pending and retried
  automatically on the next poll, instead of silently reverting in the UI
- Per-unit mode configuration: units that only support on/off (no cool/heat
  selection) can be configured to hide the mode picker while still exposing
  temperature and fan control

## Requirements

- Home Assistant's built-in `bluetooth` integration set up with a BLE
  adapter (local adapter or a remote BLE proxy) that can reach the units
- No manual pairing needed — see [Bluetooth Pairing](#bluetooth-pairing)
  below

## Installation

### HACS (custom repository)

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add this repository URL, category **Integration**
3. Install **Madoka**, then restart Home Assistant

### Manual

Copy this repository's contents into a `custom_components/madoka/`
directory in your Home Assistant config directory, then restart Home
Assistant.

## Configuration

Configuration is done entirely from the UI — there is no YAML setup.

1. **Settings → Devices & Services → Add Integration → Madoka**
2. Enter your devices as a list in the YAML editor:

```yaml
- name: Living Room
  mac: AA:BB:CC:DD:EE:FF
  modes: [off, cool, heat]
- name: Bedroom
  mac: "11:22:33:44:55:66"
  modes: [off, auto]
```

Each device needs a `name` and `mac`. `modes` is optional and defaults to
`[off, cool, heat]`:

- `[off, cool, heat]` — full control; HA lets you pick Cool or Heat.
- `[off, auto]` — for units that only support power on/off. HA shows a
  single "on" mode (`auto`) with no cool/heat picker; turning the unit on
  leaves whatever mode is already set on the device untouched. Temperature
  and fan speed are still fully controllable.

Only one Madoka config entry is supported; all of your devices live in that
single entry.

### Updating devices later

Go to **Settings → Devices & Services → Madoka → Configure** to edit the
device list (add/remove units, change `modes`, etc.). Saving reloads the
integration automatically.

## Bluetooth Pairing

Pairing is automatic — there's no `bluetoothctl` or any other manual command
to run. The integration registers its own BlueZ pairing agent and pairs each
device the first time it connects.

The controller itself will display a numeric code and wait for you to
confirm it on the unit, the same as pairing any other Bluetooth accessory.
This happens once per device: the first time it connects after being added,
or after its pairing is removed. Watch the unit and confirm the code when it
appears.

First-time pairing can take up to about a minute, and the integration
automatically retries if the first attempt doesn't finish in time — that
delay is normal, not a fault. After that, reconnects (including across Home
Assistant restarts) are instant and need no further interaction.

Devices connect and pair one at a time, never in parallel, so if you have
several units all needing to pair for the first time, expect the unit
prompts to appear one after another rather than all at once — with multiple
units that can add up to a few minutes in total.

If a device isn't connecting specifically because it's waiting on that
pairing confirmation (rather than being out of range or powered off) — for
example, taking a minute to notice the prompt on first setup, or if Home
Assistant later happens to connect through a different Bluetooth adapter
than the one a device was previously paired on — you'll see a Repair under
**Settings → System → Repairs** naming the device. It clears itself
automatically once the device connects.

The integration also fires a `madoka_pairing_required` event (and
`madoka_pairing_resolved` once resolved) if you'd rather be notified more
directly, e.g. with a push notification:

```yaml
automation:
  - alias: "Madoka pairing needed"
    trigger:
      - platform: event
        event_type: madoka_pairing_required
    action:
      - service: notify.mobile_app_<your_device>
        data:
          title: "AC needs pairing"
          message: "{{ trigger.event.data.name }} needs pairing confirmed on the unit."
```

## Development

The BLE wire protocol is split into two layers:

- `decoder.py` - the field <-> TLV mapping table (e.g. `"mode"` <-> type
  `0x20` under cmd `0x30`, with `"cool"` <-> `3`).
- `protocol.py` - the full dict <-> binary-frame codec built on top of it
  (`encode()` turns `{"mode": "cool"}` into ready-to-write chunks;
  `FrameParser` reassembles incoming chunks back into `(cmd, dict)`).

Neither has a Home Assistant or `bleak` dependency, so both are covered by a
plain-`unittest` suite that runs without either installed:

```bash
python3 -m unittest discover -v
```

`framer.py` wraps `protocol.py` with the async/threading plumbing needed for
a real BLE connection (the notification queue, thread safety for BLE
callbacks, response-timeout handling) and isn't covered by tests, since that
part inherently needs a running event loop rather than being pure logic.

## Limitations

- `local_polling`: state is refreshed on Home Assistant's normal entity poll
  interval, not pushed instantly from the device
- BLE range and adapter reliability directly affect responsiveness; units
  that go out of range will show as unavailable until reconnected
- Bluetooth adapters have a limited number of simultaneous connections -
  commonly around 5 for CSR-based adapters, 7 for Broadcom - so if you have
  more Madoka units than your adapter supports, you'll need an additional
  Bluetooth adapter. Home Assistant supports multiple local adapters and
  will spread connections across them automatically.

## Credits

The BLE protocol was reverse-engineered by [blafois](https://github.com/blafois),
documented in
[Daikin-Madoka-BRC1H-BLE-Reverse](https://github.com/blafois/Daikin-Madoka-BRC1H-BLE-Reverse)
and also implemented as an
[OpenHAB binding](https://github.com/openhab/openhab-addons/tree/main/bundles/org.openhab.binding.bluetooth.daikinmadoka).
This integration wouldn't exist without that work.
