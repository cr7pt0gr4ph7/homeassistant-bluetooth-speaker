"""Modified Bluetooth client implementation from the bleak library"""

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    if sys.platform != "linux":
        assert False, "This backend is only available on Linux"

import asyncio
import logging
import os
import warnings
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any, Optional, Union

if sys.version_info < (3, 12):
    from typing_extensions import Buffer, override
else:
    from collections.abc import Buffer
    from typing import override

if sys.version_info < (3, 11):
    from async_timeout import timeout as async_timeout
else:
    from asyncio import timeout as async_timeout

from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType, ErrorType, MessageType
from dbus_fast.message import Message
from dbus_fast.signature import Variant

from bleak import BleakScanner
from bleak.backends.bluezdbus import defs
from bleak.backends.bluezdbus.manager import BlueZManager, get_global_bluez_manager
from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus
from bleak.backends.bluezdbus.utils import assert_reply, get_dbus_authenticator
from bleak.backends.bluezdbus.version import BlueZFeatures
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.client import BaseBleakClient, NotifyCallback
from bleak.backends.descriptor import BleakGATTDescriptor
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

logger = logging.getLogger(__name__)

# prevent tasks from being garbage collected
_background_tasks = set[asyncio.Task[None]]()


class BluetoothClientBlueZDBus(BaseBleakClient):
    """A native Linux Bluetooth client.

    Implemented by using the `BlueZ DBUS API <https://docs.ubuntu.com/core/en/stacks/bluetooth/bluez/docs/reference/dbus-api>`_.

    This is basically stripped-down version of the BlueZ backend from the
    `bleak <https://github.com/hbldh/bleak>` library, with some necessary changes:

    While Bleak assumes that it fully manages the state of the device
    (so it isn't connected before a `BleakClient` connects to it, and
    will be disconnected when the `BleakClient` is destroyed), we want
    our implementation to be basically stateless, and defer to the BlueZ
    daemon for the actual state.

    Args:
        address_or_ble_device (`BLEDevice` or str): The Bluetooth address of the BLE peripheral to connect to or the `BLEDevice` object representing it.

    Keyword Args:
        timeout (float): Timeout for required ``BleakScanner.find_device_by_address`` call. Defaults to 10.0.
        disconnected_callback (callable): Callback that will be scheduled in the
            event loop when the client is disconnected. The callable must take one
            argument, which will be this client object.
        adapter (str): Bluetooth adapter to use for discovery.
    """

    def __init__(
        self,
        address_or_ble_device: Union[BLEDevice, str],
        **kwargs: Any,
    ):
        super(BluetoothClientBlueZDBus, self).__init__(address_or_ble_device, **kwargs)
        # kwarg "device" is for backwards compatibility
        self._adapter: Optional[str] = kwargs.get("adapter", kwargs.get("device"))

        self._device_info: Optional[dict[str, Any]]

        # Backend specific, D-Bus objects and data
        if isinstance(address_or_ble_device, BLEDevice):
            self._device_path = address_or_ble_device.details["path"]
            self._device_info = address_or_ble_device.details.get("props")
        else:
            self._device_path = None
            self._device_info = None

        # D-Bus message bus
        self._bus: Optional[MessageBus] = None
        # tracks device watcher subscription
        self._remove_device_watcher: Optional[Callable[[], None]] = None
        # private backing for is_connected property
        self._is_connected = False
        # indicates disconnect request in progress when not None
        self._disconnecting_event: Optional[asyncio.Event] = None
        # used to ensure device gets disconnected if event loop crashes
        self._disconnect_monitor_event: Optional[asyncio.Event] = None

    # Setup methods

    async def setup(self, **kwargs: Any) -> None:
        """Connect to BlueZ to get information for the specified Bluetooth device.

        Keyword Args:
            timeout (float): Timeout for required ``BleakScanner.find_device_by_address`` call. Defaults to 10.0.

        Raises:
            BleakError: If the device could not be found.
            BleakDBusError: If there was a D-Bus error
            asyncio.TimeoutError: If the connection timed out
        """
        timeout = kwargs.get("timeout", self._timeout)
        await self._ensure_device_resolved(timeout)

        # receive connection state changes
        manager = await get_global_bluez_manager()
        self._setup_device_watcher(manager)
        self._is_connected = manager.is_connected(self._device_path)

    def close(self) -> None:
        """Frees all resources."""
        self._cleanup_all()

    # Internal methods

    async def _ensure_device_resolved(self, timeout: Any) -> None:
        if self._device_path is None:
            device = await BleakScanner.find_device_by_address(
                self.address,
                timeout=timeout,
                adapter=self._adapter,
                backend=BleakScannerBlueZDBus,
            )

            if device:
                self._device_info = device.details.get("props")
                self._device_path = device.details["path"]
            else:
                raise BleakDeviceNotFoundError(
                    self.address, f"Device with address {self.address} was not found."
                )

    async def _ensure_dbus_connection(self) -> None:
        """Ensure that we are connected to D-Bus."""
        if not self._bus:
            async with AsyncExitStack() as stack:
                await self._setup_dbus_connection(stack)
                stack.pop_all()

    async def _setup_dbus_connection(self, stack: AsyncExitStack) -> None:
        """Establish connection to D-Bus."""
        assert not self._bus

        self._bus = await MessageBus(
            bus_type=BusType.SYSTEM,
            negotiate_unix_fd=True,
            auth=get_dbus_authenticator(),
        ).connect()

        stack.callback(self._cleanup_bus)

    def _setup_device_watcher(self, manager: BlueZManager):
        """Register for device notifications."""
        assert not self._remove_device_watcher

        def on_connected_changed(connected: bool) -> None:
            if not connected:
                logger.debug("Device disconnected (%s)", self._device_path)

                self._is_connected = False

                if self._disconnect_monitor_event:
                    self._disconnect_monitor_event.set()
                    self._disconnect_monitor_event = None

                if self._disconnected_callback is not None:
                    self._disconnected_callback()
                disconnecting_event = self._disconnecting_event
                if disconnecting_event:
                    disconnecting_event.set()

        def on_characteristic_value_changed(char_path: str, value: bytes) -> None:
            # We don't actually care about GATT characteristics in
            # BluetoothClientBlueZDBus, this is just a stub for
            # compatibility with the existing Bleak infrastructure.
            pass

        watcher = manager.add_device_watcher(
            self._device_path,
            on_connected_changed,
            on_characteristic_value_changed,
        )
        self._remove_device_watcher = lambda: manager.remove_device_watcher(
            watcher
        )

    # Connectivity methods

    @override
    async def connect(
        self, pair: bool, **kwargs: Any
    ) -> None:
        """Connect to the specified Bluetooth device.

        Keyword Args:
            timeout (float): Timeout for required ``BleakScanner.find_device_by_address`` call. Defaults to 10.0.

        Raises:
            BleakError: If the device is already connected or if the device could not be found.
            BleakDBusError: If there was a D-Bus error
            asyncio.TimeoutError: If the connection timed out
        """
        logger.debug("Connecting to device @ %s", self.address)

        if self.is_connected:
            raise BleakError("Client is already connected")

        if not BlueZFeatures.checked_bluez_version:
            await BlueZFeatures.check_bluez_version()
        if not BlueZFeatures.supported_version:
            raise BleakError("Bleak requires BlueZ >= 5.55.")
        # A Discover must have been run before connecting to any devices.
        # Find the desired device before trying to connect.
        timeout = kwargs.get("timeout", self._timeout)
        await self._ensure_device_resolved(timeout)

        manager = await get_global_bluez_manager()

        async with async_timeout(timeout):
            while True:
                async with AsyncExitStack() as stack:
                    # Each BLE connection session needs a new D-Bus connection to avoid a
                    # BlueZ quirk where notifications are automatically enabled on reconnect.
                    await self._setup_dbus_connection(stack)

                    self._disconnect_monitor_event = local_disconnect_monitor_event = (
                        asyncio.Event()
                    )

                    # this effectively cancels the disconnect monitor in case the event
                    # was not triggered by a D-Bus callback
                    stack.callback(local_disconnect_monitor_event.set)

                    async def disconnect_device() -> None:
                        # Calling Disconnect cancels any pending connect request. Also,
                        # if connection was successful but _get_services() raises (e.g.
                        # because task was cancelled), then we still need to disconnect
                        # before passing on the exception.
                        if self._bus:
                            # If disconnected callback already fired, this will be a no-op
                            # since self._bus will be None and the _cleanup_all call will
                            # have already disconnected.
                            try:
                                disconnect_reply = await self._bus.call(
                                    Message(
                                        destination=defs.BLUEZ_SERVICE,
                                        interface=defs.DEVICE_INTERFACE,
                                        path=self._device_path,
                                        member="Disconnect",
                                    )
                                )
                                assert disconnect_reply
                                try:
                                    assert_reply(disconnect_reply)
                                except BleakDBusError as e:
                                    # if the object no longer exists, then we know we
                                    # are disconnected for sure, so don't need to log a
                                    # warning about it
                                    if e.dbus_error != ErrorType.UNKNOWN_OBJECT.value:
                                        raise
                            except Exception as e:
                                logger.warning(
                                    f"Failed to cancel connection ({self._device_path}): {e}"
                                )

                    stack.push_async_callback(disconnect_device)

                    # The BlueZ backend does not disconnect devices when the
                    # application closes or crashes. This can cause problems
                    # when trying to reconnect to the same device. To work
                    # around this, we check if the device is already connected.
                    #
                    # For additional details see https://github.com/bluez/bluez/issues/89
                    if manager.is_connected(self._device_path):
                        logger.debug(
                            'skipping calling "Connect" since %s is already connected',
                            self._device_path,
                        )
                    else:
                        if not await self._connect_to_device(pair, local_disconnect_monitor_event, manager):
                            # Jump way back to the `while True:`` to retry.
                            continue

                    self._is_connected = True

                    # Create a task that runs until the device is disconnected.
                    task = asyncio.create_task(
                        self._disconnect_monitor(
                            self._bus,
                            self._device_path,
                            local_disconnect_monitor_event,
                        )
                    )
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)

                    stack.pop_all()
                    return

    async def _connect_to_device(self, pair: bool, local_disconnect_monitor_event: asyncio.Event, manager: BlueZManager) -> bool:
        """
        Requests BlueZ to connect to the device.

        Returns false when the connection sequence should be retried due to a transient error.
        """
        logger.debug("Connecting to BlueZ path %s", self._device_path)
        assert self._bus

        # Calling pair will fail if we are already paired, so
        # in that case we just call Connect.
        if pair and not manager.is_paired(self._device_path):
            # Trust means device is authorized
            reply = await self._bus.call(
                Message(
                    destination=defs.BLUEZ_SERVICE,
                    path=self._device_path,
                    interface=defs.PROPERTIES_INTERFACE,
                    member="Set",
                    signature="ssv",
                    body=[
                        defs.DEVICE_INTERFACE,
                        "Trusted",
                        Variant("b", True),
                    ],
                )
            )
            assert reply
            assert_reply(reply)

            # REVIST: This leaves "Trusted" property set if we
            # fail later. Probably not a big deal since we were
            # going to trust it anyway.

            # Pairing means device is authenticated
            reply = await self._bus.call(
                Message(
                    destination=defs.BLUEZ_SERVICE,
                    interface=defs.DEVICE_INTERFACE,
                    path=self._device_path,
                    member="Pair",
                )
            )

            # For resolvable private addresses, the address will
            # change after pairing, so we need to update that.
            # Hopefully there is no race condition here. D-Bus
            # traffic capture shows that Address change happens
            # at the same time as Paired property change and
            # that PropertiesChanged signal is sent before the
            # "Pair" reply is sent.
            self.address = manager.get_device_address(self._device_path)
        else:
            reply = await self._bus.call(
                Message(
                    destination=defs.BLUEZ_SERVICE,
                    interface=defs.DEVICE_INTERFACE,
                    path=self._device_path,
                    member="Connect",
                )
            )
        assert reply

        if reply.message_type == MessageType.ERROR:
            # This error is often caused by RF interference
            # from other Bluetooth or Wi-Fi devices. In many
            # cases, retrying will connect successfully.
            # Note: this error was added in BlueZ 6.62.
            if (
                reply.error_name == "org.bluez.Error.Failed"
                and reply.body
                and reply.body[0] == "le-connection-abort-by-local"
            ):
                logger.debug(
                    "retry due to le-connection-abort-by-local"
                )

                # When this error occurs, BlueZ actually
                # connected so we get "Connected" property changes
                # that we need to wait for before attempting
                # to connect again.
                await local_disconnect_monitor_event.wait()

                # Jump way back to the `while True:`` to retry.
                return False

            if reply.error_name == ErrorType.UNKNOWN_OBJECT.value:
                raise BleakDeviceNotFoundError(
                    self.address,
                    f"Device with address {self.address} was not found. It may have been removed from BlueZ when scanning stopped.",
                )

        assert_reply(reply)
        return True

    @staticmethod
    async def _disconnect_monitor(
        bus: MessageBus, device_path: str, disconnect_monitor_event: asyncio.Event
    ) -> None:
        # This task runs until the device is disconnected. If the task is
        # cancelled, it probably means that the event loop crashed so we
        # try to disconnected the device. Otherwise BlueZ will keep the device
        # connected even after Python exits. This will only work if the event
        # loop is called with asyncio.run() or otherwise runs pending tasks
        # after the original event loop stops. This will also cause an exception
        # if a run loop is stopped before the device is disconnected since this
        # task will still be running and asyncio complains if a loop with running
        # tasks is stopped.
        try:
            await disconnect_monitor_event.wait()
        except asyncio.CancelledError:
            automatic_disconnect = False
            if automatic_disconnect:
                try:
                    # by using send() instead of call(), we ensure that the message
                    # gets sent, but we don't wait for a reply, which could take
                    # over one second while the device disconnects.
                    await bus.send(
                        Message(
                            destination=defs.BLUEZ_SERVICE,
                            path=device_path,
                            interface=defs.DEVICE_INTERFACE,
                            member="Disconnect",
                        )
                    )
                except Exception:
                    pass

    def _cleanup_all(self) -> None:
        """
        Free all the allocated resource in DBus. Use this method to
        eventually cleanup all otherwise leaked resources.
        """
        logger.debug("_cleanup_all(%s)", self._device_path)
        self._cleanup_device_watcher()
        self._cleanup_bus()

    def _cleanup_device_watcher(self) -> None:
        """Unregister the device watcher."""
        logger.debug("_cleanup_device_watcher(%s)", self._device_path)

        if self._remove_device_watcher:
            self._remove_device_watcher()
            self._remove_device_watcher = None

    def _cleanup_bus(self) -> None:
        """
        Free all the allocated resource in DBus. Use this method to
        eventually cleanup all otherwise leaked resources.
        """
        logger.debug("_cleanup_bus(%s)", self._device_path)

        if not self._bus:
            logger.debug("already disconnected (%s)", self._device_path)
            return

        # Try to disconnect the System Bus.
        try:
            self._bus.disconnect()
        except Exception as e:
            logger.error(
                "Attempt to disconnect system bus failed (%s): %s",
                self._device_path,
                e,
            )
        else:
            # Critical to remove the `self._bus` object here to since it was
            # closed above. If not, calls made to it later could lead to
            # a stuck client.
            self._bus = None

    @override
    async def disconnect(self) -> None:
        """Disconnect from the specified Bluetooth device.

        Raises:
            BleakDBusError: If there was a D-Bus error
            asyncio.TimeoutError if the device was not disconnected within 10 seconds
        """
        logger.debug("Disconnecting ({%s})", self._device_path)

        if self._bus is None:
            # No connection exists. Either one hasn't been created or
            # we have already called disconnect and closed the D-Bus
            # connection.
            logger.debug("already disconnected ({%s})", self._device_path)
            return

        if self._disconnecting_event:
            # another call to disconnect() is already in progress
            logger.debug("already in progress ({%s})", self._device_path)
            async with async_timeout(10):
                await self._disconnecting_event.wait()
        elif self.is_connected:
            self._disconnecting_event = asyncio.Event()
            try:
                # Try to disconnect the actual device/peripheral
                reply = await self._bus.call(
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path=self._device_path,
                        interface=defs.DEVICE_INTERFACE,
                        member="Disconnect",
                    )
                )
                assert reply
                assert_reply(reply)
                async with async_timeout(10):
                    await self._disconnecting_event.wait()
            finally:
                self._disconnecting_event = None

        # sanity check to make sure _cleanup_all() was triggered by the
        # "PropertiesChanged" signal handler and that it completed successfully
        assert self._bus is None

    @override
    async def pair(self, *args: Any, **kwargs: Any) -> None:
        """Pair with the peripheral.

        You can use ConnectDevice method if you already know the MAC address of the device.
        Else you need to StartDiscovery, Trust, Pair and Connect in sequence.
        """
        assert self._bus
        # See if it is already paired.
        reply = await self._bus.call(
            Message(
                destination=defs.BLUEZ_SERVICE,
                path=self._device_path,
                interface=defs.PROPERTIES_INTERFACE,
                member="Get",
                signature="ss",
                body=[defs.DEVICE_INTERFACE, "Paired"],
            )
        )
        assert reply
        assert_reply(reply)
        if reply.body[0].value:
            logger.debug("Bluetooth device @ %s is already paired", self.address)
            return

        # Set device as trusted.
        reply = await self._bus.call(
            Message(
                destination=defs.BLUEZ_SERVICE,
                path=self._device_path,
                interface=defs.PROPERTIES_INTERFACE,
                member="Set",
                signature="ssv",
                body=[defs.DEVICE_INTERFACE, "Trusted", Variant("b", True)],
            )
        )
        assert reply
        assert_reply(reply)

        logger.debug("Pairing to Bluetooth device @ %s", self.address)

        reply = await self._bus.call(
            Message(
                destination=defs.BLUEZ_SERVICE,
                path=self._device_path,
                interface=defs.DEVICE_INTERFACE,
                member="Pair",
            )
        )
        assert reply
        assert_reply(reply)

        # For resolvable private addresses, the address will
        # change after pairing, so we need to update that.
        # Hopefully there is no race condition here. D-Bus
        # traffic capture shows that Address change happens
        # at the same time as Paired property change and
        # that PropertiesChanged signal is sent before the
        # "Pair" reply is sent.
        manager = await get_global_bluez_manager()
        self.address = manager.get_device_address(self._device_path)

    @override
    async def unpair(self) -> None:
        """Unpair with the peripheral."""
        adapter_path = await self._get_adapter_path()
        device_path = await self._get_device_path()
        manager = await get_global_bluez_manager()

        logger.debug(
            "Removing BlueZ device path %s from adapter path %s",
            device_path,
            adapter_path,
        )

        # If this client object wants to connect again, BlueZ needs the device
        # to follow Discovery process again - so reset the local connection
        # state.
        #
        # (This is true even if the request to RemoveDevice fails,
        # so clear it before.)
        self._device_path = None
        self._device_info = None
        self._is_connected = False

        assert manager._bus

        try:
            reply = await manager._bus.call(
                Message(
                    destination=defs.BLUEZ_SERVICE,
                    path=adapter_path,
                    interface=defs.ADAPTER_INTERFACE,
                    member="RemoveDevice",
                    signature="o",
                    body=[device_path],
                )
            )
            assert reply
            assert_reply(reply)
        except BleakDBusError as e:
            if e.dbus_error == "org.bluez.Error.DoesNotExist":
                raise BleakDeviceNotFoundError(
                    self.address, f"Device with address {self.address} was not found."
                ) from e
            raise

    @property
    @override
    def is_connected(self) -> bool:
        """Check connection status between this client and the server.

        Returns:
            Boolean representing connection status.

        """
        return False if self._bus is None else self._is_connected

    async def _get_adapter_path(self) -> str:
        """Private coroutine to return the BlueZ path to the adapter this client is assigned to.

        Can be called even if no connection has been established yet.
        """
        if self._device_info:
            # If we have a BlueZ DBus object with _device_info, use what it tell us
            return self._device_info["Adapter"]
        if self._adapter:
            # If the adapter name was set in the constructor, convert to a BlueZ path
            return f"/org/bluez/{self._adapter}"

        # Fall back to the system's default Bluetooth adapter
        manager = await get_global_bluez_manager()
        return manager.get_default_adapter()

    async def _get_device_path(self) -> str:
        """Private coroutine to return the BlueZ path to the device address this client is assigned to.

        Unlike the _device_path property, this function can be called even if discovery process has not
        started and/or connection has not been established yet.
        """
        if self._device_path:
            # If we have a BlueZ DBus object, return its device path
            return self._device_path

        # Otherwise, build a new path using the adapter path and the BLE address
        adapter_path = await self._get_adapter_path()
        bluez_address = self.address.upper().replace(":", "_")
        return f"{adapter_path}/dev_{bluez_address}"

    @property
    @override
    def name(self) -> str:
        """See :meth:`bleak.BleakClient.name`."""
        if self._device_info is None:
            raise BleakError("Not connected")
        return self._device_info["Alias"]

    @property
    @override
    def mtu_size(self) -> int:
        """Stub methods"""
        raise NotImplementedError()

    @override
    async def read_gatt_char(
        self, characteristic: BleakGATTCharacteristic, **kwargs: Any
    ) -> bytearray:
        """Stub method."""
        raise NotImplementedError()

    @override
    async def read_gatt_descriptor(
        self, descriptor: BleakGATTDescriptor, **kwargs: Any
    ) -> bytearray:
        """Stub method."""
        raise NotImplementedError()

    @override
    async def write_gatt_char(
        self, characteristic: BleakGATTCharacteristic, data: Buffer, response: bool
    ) -> None:
        raise NotImplementedError()

    @override
    async def write_gatt_descriptor(
        self, descriptor: BleakGATTDescriptor, data: Buffer
    ) -> None:
        """Stub method."""
        raise NotImplementedError()

    @override
    async def start_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback: NotifyCallback,
        **kwargs: Any,
    ) -> None:
        """Stub method."""
        raise NotImplementedError()

    @override
    async def stop_notify(self, characteristic: BleakGATTCharacteristic) -> None:
        """Stub method."""
        raise NotImplementedError()
