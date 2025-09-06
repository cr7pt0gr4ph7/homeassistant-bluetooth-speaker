"""Support for Bluetooth speakers."""

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BluetoothSpeakerConfigEntry, BluetoothSpeakerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: BluetoothSpeakerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Bluetooth speaker config entry."""
    coordinator = config_entry.runtime_data
    async_add_entities([
        BluetoothSpeakerPlayer(
            config_entry.data[CONF_NAME],
            coordinator,
        )
    ])


class BluetoothSpeakerPlayer(CoordinatorEntity[BluetoothSpeakerCoordinator], MediaPlayerEntity):
    """A media player controlling a Bluetooth speaker."""

    _attr_should_poll = False
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(self, name: str, coordinator: BluetoothSpeakerCoordinator) -> None:
        """Initialize the Bluetooth speaker device."""
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = format_mac(coordinator.device.address)
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, coordinator.device.address)},
            connections={(dr.CONNECTION_BLUETOOTH, coordinator.device.address)},
        )
        self._attr_volume_level = 1.0
        self._attr_is_volume_muted = False

    @property
    def state(self) -> MediaPlayerState:
        """State of the player."""
        return MediaPlayerState.ON if self.coordinator.device.is_connected else MediaPlayerState.OFF

    async def async_turn_on(self) -> None:
        """Connect to the Bluetooth speaker."""
        await self.coordinator.device.connect()

    async def async_turn_off(self) -> None:
        """Disconnect from the Bluetooth speaker."""
        await self.coordinator.device.disconnect()

    def mute_volume(self, mute: bool) -> None:
        """Mute the Bluetooth speaker."""
        self._attr_is_volume_muted = mute
        self.schedule_update_ha_state()

    def volume_up(self) -> None:
        """Increase volume."""
        assert self.volume_level is not None
        self._attr_volume_level = min(1.0, self.volume_level + 0.1)
        self.schedule_update_ha_state()

    def volume_down(self) -> None:
        """Decrease volume."""
        assert self.volume_level is not None
        self._attr_volume_level = max(0.0, self.volume_level - 0.1)
        self.schedule_update_ha_state()

    def set_volume_level(self, volume: float) -> None:
        """Set the volume level, range 0..1."""
        self._attr_volume_level = volume
        self.schedule_update_ha_state()
