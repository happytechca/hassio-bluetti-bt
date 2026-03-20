"""Bluetti BT number entities (sliders)."""

from __future__ import annotations
import asyncio
import logging
import async_timeout
from bleak import BleakScanner
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from bluetti_bt_lib import build_device, BluettiDevice, DeviceWriter, NumberField, FieldName

from .types import FullDeviceConfig
from . import device_info as dev_info, get_unique_id
from .const import DATA_COORDINATOR, DATA_LOCK, DOMAIN
from .coordinator import PollingCoordinator
from .utils import mac_loggable, unique_id_logable


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Setup number entities."""

    config = FullDeviceConfig.from_dict(entry.data)
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    lock = hass.data[DOMAIN][entry.entry_id][DATA_LOCK]

    logger = logging.getLogger(
        f"{__name__}.{mac_loggable(config.address).replace(':', '_')}"
    )

    if config is None or not isinstance(coordinator, PollingCoordinator):
        logger.error("No coordinator found")
        return None

    logger.info("Creating number entities for device with address %s", config.address)
    device_info = dev_info(entry)

    bluetti_device = build_device(config.name)

    entities_to_add = []
    for field in bluetti_device.get_number_fields():
        entities_to_add.append(
            BluettiNumber(
                bluetti_device,
                config.address,
                coordinator,
                device_info,
                field,
                lock,
                use_encryption=config.use_encryption,
                logger=logger,
            )
        )

    async_add_entities(entities_to_add)


class BluettiNumber(CoordinatorEntity, NumberEntity):
    """Bluetti universal number entity."""

    def __init__(
        self,
        bluetti_device: BluettiDevice,
        address: str,
        coordinator: PollingCoordinator,
        device_info: DeviceInfo,
        field: NumberField,
        lock: asyncio.Lock,
        use_encryption: bool = False,
        logger: logging.Logger = logging.getLogger(),
    ):
        """Init entity."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._logger = logger

        e_name = f"{device_info.get('name')} {field.name}"
        self._bluetti_device = bluetti_device
        self._address = address
        self._field = field
        self._response_key = field.name
        self._unavailable_counter = 5
        self._lock = lock
        self._use_encryption = use_encryption

        self._attr_has_entity_name = True
        self._attr_device_info = device_info
        self._attr_translation_key = field.name
        self._attr_available = False
        self._attr_unique_id = get_unique_id(e_name)
        self._attr_mode = NumberMode.SLIDER
        self._attr_native_step = 1
        self._attr_native_min_value = field.min if field.min is not None else 0
        self._attr_native_max_value = field.max if field.max is not None else 100

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._attr_available

    def _set_available(self):
        self._attr_available = True
        self._unavailable_counter = 0
        self._attr_extra_state_attributes = {}
        self.async_write_ha_state()

    def _set_unavailable(self, cause: str = "Unknown"):
        self._unavailable_counter += 1
        self._attr_extra_state_attributes = {
            "unavailable_counter": self._unavailable_counter,
            "unavailable_cause": cause,
        }
        if self._unavailable_counter >= 5:
            self._attr_available = False
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        if self.coordinator.data is None:
            self._set_unavailable("Data is None")
            return

        if not isinstance(self.coordinator.data, dict):
            self._set_unavailable("Invalid data")
            return

        response_data = self.coordinator.data.get(self._response_key)
        if response_data is None:
            self._set_unavailable("No data")
            return

        if not isinstance(response_data, (int, float)):
            self._logger.warning(
                "Invalid response data type from coordinator (number.%s): %s",
                unique_id_logable(self._attr_unique_id),
                response_data,
            )
            self._set_unavailable("Invalid data type")
            return

        self._set_available()
        self._attr_native_value = response_data
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        # Guard: SOC min must be less than SOC max
        data = self.coordinator.data or {}
        if self._field.name == FieldName.BATTERY_SOC_RANGE_START.value:
            soc_max = data.get(FieldName.BATTERY_SOC_RANGE_END.value)
            if soc_max is not None and value >= soc_max:
                self._logger.warning("SOC Min (%s) must be less than SOC Max (%s)", value, soc_max)
                return
        elif self._field.name == FieldName.BATTERY_SOC_RANGE_END.value:
            soc_min = data.get(FieldName.BATTERY_SOC_RANGE_START.value)
            if soc_min is not None and value <= soc_min:
                self._logger.warning("SOC Max (%s) must be greater than SOC Min (%s)", value, soc_min)
                return

        self._logger.debug(
            "Set %s on %s to %s", self._response_key, mac_loggable(self._address), value
        )
        await self.write_to_device(value)

    async def write_to_device(self, value: float):
        """Write to device."""

        try:
            if self._use_encryption:
                await self.coordinator.reader.write(self._field.name, value)
            else:
                device = await BleakScanner.find_device_by_address(self._address, timeout=5)

                if device is None:
                    return

                client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    device.name or "Unknown Device",
                    max_attempts=10,
                )

                if not client.is_connected:
                    return

                writer = DeviceWriter(client, self._bluetti_device, lock=self._lock)

                async with async_timeout.timeout(15):
                    await writer.write(self._field.name, int(value))
                    await asyncio.sleep(5)

        except TimeoutError:
            self._logger.error("Timed out for device %s", mac_loggable(self._address))
            return None

        await self.coordinator.async_request_refresh()
