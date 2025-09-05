# Home Assistant Bluetooth Speaker integration

## 🚧 WARNING: This is currently an incomplete Work in Progress, and may not work at all, cause unepxected errors etc.!

This integration allows connecting Bluetooth speakers to the Home Assistant server.

# Implementation

This integration currently only works on Linux, as it is intended for use with Home Assistant OS.

Seeing that there do not currently does not seem to be an actively maintained Python library for accessing and managing non-BLE Bluetooth devices, we directly access the BlueZ Bluetooth manager daemon via DBus instead.

In the future, we might opt to extract that code into a separate Python package, as per the [Home Assistant developer checklist](https://developers.home-assistant.io/docs/development_checklist).
Possible locations for those libraries include
the [`Bluetooth-Devices`](https://github.com/Bluetooth-Devices) (which already hosts [`bluetooth-adapters`](https://github.com/bluetooth-devices/bluetooth-adapters) and [`dbus-fast`](https://github.com/Bluetooth-Devices/dbus-fast) (which `bleak` uses on Linux))
and [`home-assistant-libs`](https://github.com/home-assistant-libs) (which already hosts [`home-assistant-bluetooth`](https://github.com/home-assistant-libs/home-assistant-bluetooth))
organizations on GitHub.

Related libraries and why we're not using them:

- [`pybluez`](https://github.com/pybluez/pybluez) supports non-BLE devices,
  but is marked as unmaintained and its last update was in 2023.

- [`habluetooth`](https://github.com/Bluetooth-Devices/habluetooth/) (which in turn uses [`bleak`](https://github.com/hbldh/bleak))
  are multi-platform and support discovery of BLE (Bluetooth Low Enery) and non-BLE devices,
  but do not support connection management for non-BLE devices.

  We're actually using `habluetooth`/`bleak` under the hood for the discovery of our Bluetooth devices,
  as that is what the [`bluetooth` integration](https://www.home-assistant.io/integrations/bluetooth)
  uses for scanning & discovery, and switch over our custom DBus code for connecting to BlueZ.

- [PyQt](https://www.riverbankcomputing.com/software/pyqt/) bindings for cross-platform [QtBluetooth](https://www.riverbankcomputing.com/static/Docs/PyQt6/api/qtbluetooth/qtbluetooth-module.html) API:
  It's a large native library to pull in just to get access to a small API.

# Installation

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

#### Via HACS
* Add this repo as a ["Custom repository"](https://hacs.xyz/docs/faq/custom_repositories/) with type "Integration"
* Click "Install" in the new "Customized Entity ID Generation" card in HACS.
* Install
* Restart Home Assistant

#### Manual Installation
* Copy the entire `custom_components/bluetooth_speaker/` directory to your server's `<config>/custom_components` directory
* Restart Home Assistant
