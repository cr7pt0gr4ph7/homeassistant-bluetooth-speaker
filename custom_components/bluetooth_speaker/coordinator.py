"""Support for Bluetooth speakers."""

from homeassistant.config_entries import ConfigEntry

type BluetoothSpeakerConfigEntry = ConfigEntry[BluetoothSpeakerCoordinator]

class BluetoothSpeakerCoordinator:
    """Class to handle connection to the bluetooth speaker."""
    pass
