"""Support for Bluetooth speakers."""

from __future__ import annotations

from datetime import timedelta
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .utils.bluetooth.client import BluetoothClientBlueZDBus

_LOGGER = logging.getLogger(__name__)

type BluetoothSpeakerConfigEntry = ConfigEntry[BluetoothSpeakerCoordinator]


class BluetoothSpeakerDevice:
    """Class representing the bluetooth speaker."""
    ble_device: BLEDevice
    client: BleakClient

    def __init__(self, ble_device: BLEDevice):
        """Initialize the device representation."""
        self.ble_device = ble_device
        self.client = BluetoothClientBlueZDBus(ble_device)

    async def setup(self) -> None:
        """Finish initialization."""
        await self.client.setup()

    @property
    def address(self) -> str:
        """Gets the address of the Bluetooth device."""
        return self.client.address

    @property
    def is_connected(self) -> bool:
        """Whether the Bluetooth speaker is currently connected."""
        return self.client.is_connected

    async def connect(self) -> None:
        """Attempt to connect to the Bluetooth device."""
        await self.client.connect()

    async def disconnect(self) -> None:
        """Disconnect from the Bluetooth device."""
        await self.client.disconnect()

    async def update(self) -> None:
        pass


class BluetoothSpeakerCoordinator(DataUpdateCoordinator[None]):
    """Class to handle connection to the bluetooth speaker."""
    device: BluetoothSpeakerDevice
    config_entry: BluetoothSpeakerConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BluetoothSpeakerConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        address = self.config_entry.unique_id

        assert address is not None

        self.device = BluetoothSpeakerDevice(address)

        # Subscribe to status updates
        await self.device.setup()

    async def _async_update_data(self) -> None:
        """Update status of Bluetooth speaker."""
        try:
            await self.device.update()
            self.last_update_success = True
        except Exception as err:
            self.last_update_success = False
            raise UpdateFailed(f"Unable to fetch data: {err}") from err
