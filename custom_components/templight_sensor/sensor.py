"""Platform for sensor integration."""
from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
)
from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.light import (
    ColorMode,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.helpers.entity import (
    EntityCategory,
    DeviceInfo,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant, Event, callback, CALLBACK_TYPE
from homeassistant.helpers import entity_registry, device_registry
from homeassistant.helpers.event import async_track_state_change_event, async_track_state_added_domain

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    _: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Platform setup from config flow.
    """
    _LOGGER.info("loading templights component")
    
    # Add sensors for ALL currently available lights in HA.
    lights = hass.states.async_entity_ids(LIGHT_DOMAIN)
    _LOGGER.debug("detected lights on startup: %s", ", ".join(lights))
    await create_templights(lights, hass, async_add_entities)

    # Listen for new lights added later.
    @callback
    async def on_light_added(event: Event) -> None:
        """
        Translates new entity id callback to function to create templights.
        """
        light_id: Any = event.data["entity_id"]
        _LOGGER.debug("detected new light %s", light_id)

        if not isinstance(light_id, str):
            _LOGGER.error("on_light_added got non-string entity id %s", light_id)
            return

        await create_templights(
            lights=[str(light_id)],
            hass=hass,
            async_add_entities=async_add_entities,
        )
    async_track_state_added_domain(hass, LIGHT_DOMAIN, on_light_added)

async def create_templights(
    lights: list[str],
    hass: HomeAssistant,
    async_add_entities: AddEntitiesCallback
) -> None:
    """
    Create TempLight entities for each light in the list.
    """
    new_sensors: list[SensorEntity] = []
    e_registry = entity_registry.async_get(hass)
    d_registry = device_registry.async_get(hass)

    for light_id in lights:
        # Get the entity for the light.
        light_entity = e_registry.async_get(light_id)
        if light_entity is None:
            _LOGGER.error(
                "Failed to retreive entity for %s, not adding sensor", light_id
            )
            continue

        # Get the original device for the entity.
        light_device = d_registry.async_get(device_id=light_entity.device_id)
        if light_device is None:
            _LOGGER.error(
                "Failed to retreive device for %s, entity id = %s, device id = %s, not adding sensor",
                light_id,
                light_entity.entity_id,
                light_entity.device_id,
            )
            continue

        # Create templight sensors.
        new_sensors.append(
            ColorTemperatureSensor(
                base_light_entity=light_entity,
                base_light_device=light_device,
                hass=hass,
            )
        )
        new_sensors.append(
            BrightnessSensor(
                base_light_entity=light_entity,
                base_light_device=light_device,
                hass=hass,
            )
        )
        new_sensors.append(
            ColorHueSensor(
                base_light_entity=light_entity,
                base_light_device=light_device,
                hass=hass,
            )
        )
        new_sensors.append(
            ColorSaturationSensor(
                base_light_entity=light_entity,
                base_light_device=light_device,
                hass=hass,
            )
        )

    async_add_entities(new_sensors)

class TempLightSensorBase(SensorEntity):
    """Base representation of a TempLight Sensor."""

    async_push_update: Callable[[], None]
    _unsub: CALLBACK_TYPE

    def __init__(
        self,
        base_light_entity: entity_registry.RegistryEntry,
        base_light_device: device_registry.DeviceEntry,
        hass: HomeAssistant,
        async_push_update: Callable[[], None],
    ) -> None:
        """Initialize the sensor."""
        super().__init__()
        self._base_light_entity = base_light_entity
        self._base_light_device = base_light_device
        self._hass = hass

        # Entity
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_has_entity_name = True

        # Link this new entity to the original device.
        identifiers: set[tuple[str, str]] = {
            (DOMAIN, self._base_light_entity.unique_id),
        }.union(self._base_light_device.identifiers)
        self._attr_device_info = DeviceInfo(
            # To link this entity to the cover device, this property must return an
            # identifiers value matching that used in the cover, but no other information such
            # as name. If name is returned, this entity will then also become a device in the
            # HA UI.
            identifiers=identifiers
        )
        _LOGGER.debug(
            "identifiers added to reference base light %s: %s",
            self._base_light_entity.entity_id,
            identifiers,
        )

        # Push instead of poll for state updates from base light.
        self._attr_should_poll = False
        self.async_push_update = async_push_update
        self._unsub = async_track_state_change_event(
            self._hass,
            self._base_light_entity.entity_id,
            self.async_on_base_light_change
        )
    
    async def cleanup(self) -> None:
        """Stop watching state changes to base light."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    async def async_on_base_light_change(self, _: Event) -> None:
        """Called whenever the base light state is updated."""
        _LOGGER.debug(
            "base light %s state change detected by %s",
            self._base_light_entity.entity_id,
            self.entity_id,
        )
        await self.async_push_update()

    async def async_update(self) -> None:
        """Updates if the device is enabled."""
        self._attr_available = not self._base_light_entity.disabled

    def read_attribute(self, attribute: str) -> Any:
        """Read the given attribute value from the base light."""
        base_light_state = self._hass.states.get(self._base_light_entity.entity_id)
        if base_light_state is None:
            _LOGGER.warning(
                "Failed to get state for %s, no update to %s",
                self._base_light_entity.entity_id,
                self.entity_id,
            )
            return None

        val = base_light_state.attributes.get(attribute)
        if val is None:
            _LOGGER.debug(
                "%s does not have attribute %s, no update to %s",
                self._base_light_entity.entity_id,
                attribute,
                self.entity_id,
            )
        return val


class ColorTemperatureSensor(TempLightSensorBase):
    """Sensor that extracts out the color temperature of the given light."""

    def __init__(
        self,
        base_light_entity: entity_registry.RegistryEntry,
        base_light_device: device_registry.DeviceEntry,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(base_light_entity, base_light_device, hass, self.async_update)

        # Entity
        self._attr_name = "Color temperature"
        self._attr_icon = "mdi:temperature-kelvin"
        self._attr_unique_id = (
            f"{self._base_light_entity.unique_id}_{ColorMode.COLOR_TEMP}"
        )
        self.entity_id = (
            f"{self._base_light_entity.entity_id}_{ColorMode.COLOR_TEMP}"
        )

        # SensorEntity
        self._attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Force update on setup.
        self.hass = hass
        hass.add_job(self.async_update)

    @callback
    async def async_update(self) -> None:
        """Updates the native value with the attribute (color temp)."""
        _LOGGER.debug(
            "updating color temp for %s",
            self.entity_id,
        )
        await super().async_update()

        kelvin: int | None = self.read_attribute(ATTR_COLOR_TEMP_KELVIN)
        if kelvin is None:
            self._attr_native_value = None
            return

        self._attr_native_value = kelvin
        self.async_write_ha_state()


class BrightnessSensor(TempLightSensorBase):
    """Sensor that extracts out the brightness percentage of the given light."""

    def __init__(
        self,
        base_light_entity: entity_registry.RegistryEntry,
        base_light_device: device_registry.DeviceEntry,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(base_light_entity, base_light_device, hass, self.async_update)

        # Entity
        self._attr_name = "Brightness"
        self._attr_icon = "mdi:brightness-6"
        self._attr_unique_id = (
            f"{self._base_light_entity.unique_id}_{ColorMode.BRIGHTNESS}"
        )
        self.entity_id = (
            f"{self._base_light_entity.entity_id}_{ColorMode.BRIGHTNESS}"
        )

        # SensorEntity
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Force update on setup.
        self.hass = hass
        hass.add_job(self.async_update)

    @callback
    async def async_update(self) -> None:
        """Updates the native value with the attribute (brightness)."""
        _LOGGER.debug(
            "updating brightness for %s",
            self.entity_id,
        )
        await super().async_update()

        brightness: int | None = self.read_attribute(ATTR_BRIGHTNESS)
        if brightness is None:
            self._attr_native_value = None
            return

        # 0 - 255 -> percentage rounded to whole number
        self._attr_native_value = round((brightness / 255) * 100)
        self.async_write_ha_state()

class ColorHueSensor(TempLightSensorBase):
    """Sensor that extracts out the color hue of the given light."""

    def __init__(
        self,
        base_light_entity: entity_registry.RegistryEntry,
        base_light_device: device_registry.DeviceEntry,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(base_light_entity, base_light_device, hass, self.async_update)

        # Entity
        self._attr_name = "Hue"
        self._attr_icon = "mdi:palette"
        self._attr_unique_id = (
            f"{self._base_light_entity.unique_id}_{ColorMode.HS}_hue"
        )
        self.entity_id = (
            f"{self._base_light_entity.entity_id}_{ColorMode.HS}_hue"
        )

        # SensorEntity
        self._attr_native_unit_of_measurement = "°"
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Force update on setup.
        self.hass = hass
        hass.add_job(self.async_update)

    @callback
    async def async_update(self) -> None:
        """Updates the native value with the attribute (hs)."""
        _LOGGER.debug(
            "updating hue for %s",
            self.entity_id,
        )
        await super().async_update()

        hs_color: tuple | None = self.read_attribute(ATTR_HS_COLOR)
        if hs_color is None:
            self._attr_native_value = None
            return

        if len(hs_color) != 2:
            _LOGGER.warning(
                "State for %s is unexpected hs_color %s, no update to %s",
                self._base_light_entity.entity_id,
                hs_color,
                self.entity_id,
            )
            self._attr_native_value = None
            return

        # Extract out first part as hue
        self._attr_native_value = round(hs_color[0])
        self.async_write_ha_state()

class ColorSaturationSensor(TempLightSensorBase):
    """Sensor that extracts out the color saturation of the given light."""

    def __init__(
        self,
        base_light_entity: entity_registry.RegistryEntry,
        base_light_device: device_registry.DeviceEntry,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(base_light_entity, base_light_device, hass, self.async_update)

        # Entity
        self._attr_name = "Saturation"
        self._attr_icon = "mdi:invert-colors"
        self._attr_unique_id = (
            f"{self._base_light_entity.unique_id}_{ColorMode.HS}_saturation"
        )
        self.entity_id = (
            f"{self._base_light_entity.entity_id}_{ColorMode.HS}_saturation"
        )

        # SensorEntity
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Force update on setup.
        self.hass = hass
        hass.add_job(self.async_update)

    @callback
    async def async_update(self) -> None:
        """Updates the native value with the attribute (hs)."""
        _LOGGER.debug(
            "updating hue for %s",
            self.entity_id,
        )
        await super().async_update()

        hs_color: tuple | None = self.read_attribute(ATTR_HS_COLOR)
        if hs_color is None:
            self._attr_native_value = None
            return

        if len(hs_color) != 2:
            _LOGGER.warning(
                "State for %s is unexpected hs_color %s, no update to %s",
                self._base_light_entity.entity_id,
                hs_color,
                self.entity_id,
            )
            self._attr_native_value = None
            return

        # Extract out first part as saturation
        self._attr_native_value = round(hs_color[1])
        self.async_write_ha_state()
