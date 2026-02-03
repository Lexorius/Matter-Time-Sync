"""
Matter Time Sync Integration for Home Assistant.

This module is the main entry point for the Matter Time Sync integration.
It handles:
- Integration setup and teardown
- Service registration (sync_time, sync_all, refresh_devices)
- Auto-sync timer management
- Configuration updates

The integration communicates with the Matter Server via WebSocket to synchronize
time and timezone information on Matter devices that support the Time Synchronization
cluster (0x0038).

Author: @Loweack, @Lexorius, @miketth
Version: 2.0.3 (with auto-sync timer fix)
"""
from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    SERVICE_SYNC_TIME,
    SERVICE_SYNC_ALL,
    SERVICE_REFRESH_DEVICES,
    CONF_AUTO_SYNC_ENABLED,
    CONF_AUTO_SYNC_INTERVAL,
    CONF_DEVICE_FILTER,
    CONF_ONLY_TIME_SYNC_DEVICES,
    DEFAULT_AUTO_SYNC_ENABLED,
    DEFAULT_AUTO_SYNC_INTERVAL,
    DEFAULT_DEVICE_FILTER,
    DEFAULT_ONLY_TIME_SYNC_DEVICES,
    PLATFORMS,
)
from .coordinator import MatterTimeSyncCoordinator

_LOGGER = logging.getLogger(__name__)

# =============================================================================
# SERVICE SCHEMAS
# =============================================================================
# Define the schema for the sync_time service.
# - node_id: Required, must be a positive integer (the Matter node ID)
# - endpoint: Optional, defaults to 0 (most devices use endpoint 0 for Time Sync)
SYNC_TIME_SCHEMA = vol.Schema({
    vol.Required("node_id"): cv.positive_int,
    vol.Optional("endpoint", default=0): cv.positive_int,
})


# =============================================================================
# YAML SETUP (STUB)
# =============================================================================
async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """
    Set up the component via YAML configuration.
    
    This is a stub that always returns True because this integration
    only supports UI-based configuration (config flow), not YAML.
    
    Args:
        hass: The Home Assistant instance
        config: The YAML configuration dictionary (unused)
    
    Returns:
        True to indicate successful setup
    """
    return True


# =============================================================================
# CONFIG ENTRY SETUP
# =============================================================================
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Set up Matter Time Sync from a config entry.
    
    This is the main setup function called when the integration is added
    or when Home Assistant starts. It performs the following steps:
    
    1. Initialize the coordinator (handles WebSocket communication)
    2. Parse configuration options from the config entry
    3. Store data in hass.data for access by other modules (button.py)
    4. Forward setup to platforms (creates button entities)
    5. Register services (sync_time, sync_all, refresh_devices)
    6. Start the auto-sync timer if enabled
    7. Register an update listener for configuration changes
    
    Args:
        hass: The Home Assistant instance
        entry: The config entry containing user configuration
    
    Returns:
        True if setup was successful, False otherwise
    """
    # Initialize the domain data storage if it doesn't exist
    # This dictionary holds all data for all instances of this integration
    hass.data.setdefault(DOMAIN, {})

    # -------------------------------------------------------------------------
    # STEP 1: Initialize the Coordinator
    # -------------------------------------------------------------------------
    # The coordinator manages the WebSocket connection to the Matter Server
    # and provides methods for syncing time on devices
    coordinator = MatterTimeSyncCoordinator(hass, entry)

    # -------------------------------------------------------------------------
    # STEP 2: Parse Configuration Options
    # -------------------------------------------------------------------------
    # Extract configuration values from the config entry
    # These were set by the user in the config flow UI
    
    # Device filter: comma-separated list of terms to filter devices by name
    # Example: "alpstuga, vindstyrka" -> only sync devices containing these terms
    # Empty string means all devices
    device_filter_str = entry.data.get(CONF_DEVICE_FILTER, DEFAULT_DEVICE_FILTER)
    device_filters = [f.strip().lower() for f in device_filter_str.split(",") if f.strip()]
    
    # Only sync devices that have the Time Sync cluster (0x0038)?
    # If True, devices without this cluster are skipped
    only_time_sync = entry.data.get(CONF_ONLY_TIME_SYNC_DEVICES, DEFAULT_ONLY_TIME_SYNC_DEVICES)
    
    # Auto-sync settings
    # auto_sync_enabled: Should we automatically sync all devices at intervals?
    # auto_sync_interval: How often to sync (in minutes)
    auto_sync_enabled = entry.data.get(CONF_AUTO_SYNC_ENABLED, DEFAULT_AUTO_SYNC_ENABLED)
    auto_sync_interval = entry.data.get(CONF_AUTO_SYNC_INTERVAL, DEFAULT_AUTO_SYNC_INTERVAL)

    # Log the configuration for debugging
    _LOGGER.info(
        "Matter Time Sync setup: auto_sync=%s, interval=%d min, filter=%s, only_time_sync=%s",
        auto_sync_enabled,
        auto_sync_interval,
        device_filters if device_filters else "[all]",
        only_time_sync,
    )

    # -------------------------------------------------------------------------
    # STEP 3: Store Data in hass.data
    # -------------------------------------------------------------------------
    # Store the coordinator and configuration in hass.data so that other
    # modules (like button.py) can access them
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,           # The WebSocket coordinator
        "device_filters": device_filters,     # List of filter terms
        "only_time_sync_devices": only_time_sync,  # Filter by Time Sync cluster?
        "unsub_timer": None,                  # Will hold the timer cancel function
    }

    # -------------------------------------------------------------------------
    # STEP 4: Forward Setup to Platforms
    # -------------------------------------------------------------------------
    # This loads button.py which creates button entities for each device
    # PLATFORMS is defined in const.py as ["button"]
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # -------------------------------------------------------------------------
    # STEP 5: Define and Register Service Handlers
    # -------------------------------------------------------------------------
    
    async def handle_sync_time(call: ServiceCall) -> None:
        """
        Handle the sync_time service call.
        
        This service synchronizes time on a single Matter device.
        
        Service call example:
            service: matter_time_sync.sync_time
            data:
              node_id: 7
              endpoint: 0
        
        Args:
            call: The service call containing node_id and endpoint
        """
        node_id = call.data["node_id"]
        endpoint = call.data["endpoint"]
        await coordinator.async_sync_time(node_id, endpoint)

    async def handle_sync_all(call: ServiceCall) -> None:
        """
        Handle the sync_all service call.
        
        This service synchronizes time on ALL Matter devices that match
        the configured filters (device name filter and Time Sync cluster filter).
        
        Service call example:
            service: matter_time_sync.sync_all
        
        Args:
            call: The service call (no parameters needed)
        """
        await coordinator.async_sync_all_devices()

    async def handle_refresh_devices(call: ServiceCall) -> None:
        """
        Handle the refresh_devices service call.
        
        This service scans for new Matter devices and creates button entities
        for any newly discovered devices. Useful after adding new devices
        to your Matter network.
        
        Service call example:
            service: matter_time_sync.refresh_devices
        
        Args:
            call: The service call (no parameters needed)
        """
        # Refresh the node cache from the Matter Server
        await coordinator.async_get_matter_nodes()

        # Check for new devices and add button entities for them
        from .button import async_check_new_devices
        await async_check_new_devices(hass, entry.entry_id)

    # Register the services with Home Assistant
    # We check if the service already exists to avoid duplicate registration
    # (can happen if the integration is reloaded)
    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_TIME):
        hass.services.async_register(
            DOMAIN, SERVICE_SYNC_TIME, handle_sync_time, schema=SYNC_TIME_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_ALL):
        hass.services.async_register(DOMAIN, SERVICE_SYNC_ALL, handle_sync_all)
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_DEVICES):
        hass.services.async_register(DOMAIN, SERVICE_REFRESH_DEVICES, handle_refresh_devices)

    # -------------------------------------------------------------------------
    # STEP 6: Setup Auto-Sync Timer (if enabled)
    # -------------------------------------------------------------------------
    # CRITICAL FIX: This was missing in v2.0.2!
    # The original code read the auto_sync_enabled setting but never
    # actually started a timer to perform the sync.
    if auto_sync_enabled:
        await _setup_auto_sync_timer(hass, entry, coordinator, auto_sync_interval)

    # -------------------------------------------------------------------------
    # STEP 7: Register Update Listener
    # -------------------------------------------------------------------------
    # This listener is called when the user changes the configuration
    # via Settings > Devices & Services > Matter Time Sync > Configure
    # We reload the integration to apply the new settings
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


# =============================================================================
# AUTO-SYNC TIMER SETUP
# =============================================================================
async def _setup_auto_sync_timer(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: MatterTimeSyncCoordinator,
    interval_minutes: int,
) -> None:
    """
    Set up the auto-sync timer.
    
    This function creates a periodic timer that automatically synchronizes
    time on all filtered Matter devices at the configured interval.
    
    It also runs an initial sync 30 seconds after Home Assistant starts,
    giving the Matter Server time to be fully ready.
    
    Args:
        hass: The Home Assistant instance
        entry: The config entry
        coordinator: The Matter Time Sync coordinator
        interval_minutes: How often to sync (in minutes)
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]

    # -------------------------------------------------------------------------
    # Cancel any existing timer
    # -------------------------------------------------------------------------
    # This can happen if the integration is reloaded or reconfigured
    if entry_data.get("unsub_timer"):
        entry_data["unsub_timer"]()
        entry_data["unsub_timer"] = None
        _LOGGER.debug("Cancelled existing auto-sync timer")

    # -------------------------------------------------------------------------
    # Define the timer callback function
    # -------------------------------------------------------------------------
    async def _auto_sync_callback(now) -> None:
        """
        Callback function executed at each timer interval.
        
        This function:
        1. Ensures the WebSocket connection is alive (reconnects if needed)
        2. Checks for new devices and adds buttons for them
        3. Syncs time on all filtered devices
        
        Args:
            now: The current datetime (provided by Home Assistant)
        """
        _LOGGER.info("Auto-sync triggered at %s", now)
        
        # First, ensure we have a working connection to the Matter Server
        # This will reconnect if the connection was lost
        if not await coordinator.async_ensure_connected():
            _LOGGER.error(
                "Auto-sync failed: Cannot connect to Matter Server at %s",
                coordinator._ws_url,
            )
            return

        # Check for new devices that may have been added since the last sync
        # and create button entities for them
        from .button import async_check_new_devices
        new_count = await async_check_new_devices(hass, entry.entry_id)
        if new_count > 0:
            _LOGGER.info("Auto-discovery found %d new devices", new_count)
        
        # Sync time on all devices that match the configured filters
        await coordinator.async_sync_all_devices()

    # -------------------------------------------------------------------------
    # Create the periodic timer
    # -------------------------------------------------------------------------
    # async_track_time_interval() returns a function that can be called
    # to cancel the timer. We store this function so we can cancel
    # the timer when the integration is unloaded.
    unsub = async_track_time_interval(
        hass,
        _auto_sync_callback,
        timedelta(minutes=interval_minutes),
    )
    entry_data["unsub_timer"] = unsub

    _LOGGER.info(
        "Auto-sync timer started: syncing every %d minutes",
        interval_minutes,
    )

    # -------------------------------------------------------------------------
    # Run initial sync after startup
    # -------------------------------------------------------------------------
    # We wait 30 seconds before the first sync to give Home Assistant
    # and the Matter Server add-on time to fully start
    async def _initial_sync():
        """
        Run the first sync after Home Assistant startup.
        
        This ensures devices are synced soon after startup without
        waiting for the first timer interval (which could be hours).
        """
        import asyncio
        await asyncio.sleep(30)  # Wait 30 seconds for Matter Server to be ready
        
        _LOGGER.info("Running initial auto-sync after startup")
        
        # Ensure connection first
        if not await coordinator.async_ensure_connected():
            _LOGGER.warning(
                "Initial sync skipped: Cannot connect to Matter Server. "
                "Will retry at next scheduled interval."
            )
            return
            
        await coordinator.async_sync_all_devices()

    # Start the initial sync as a background task
    # This doesn't block the setup process
    hass.async_create_task(_initial_sync())


# =============================================================================
# CONFIGURATION UPDATE LISTENER
# =============================================================================
async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Handle configuration updates.
    
    This function is called when the user changes the configuration
    via the UI (Settings > Devices & Services > Configure).
    
    We reload the entire integration to apply the new settings.
    This is the simplest and most reliable way to handle configuration changes.
    
    Args:
        hass: The Home Assistant instance
        entry: The updated config entry
    """
    _LOGGER.info("Configuration updated, reloading Matter Time Sync")
    await hass.config_entries.async_reload(entry.entry_id)


# =============================================================================
# CONFIG ENTRY UNLOAD
# =============================================================================
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Unload a config entry.
    
    This function is called when the integration is removed or when
    Home Assistant is shutting down. It performs cleanup:
    
    1. Cancel the auto-sync timer (if running)
    2. Disconnect from the Matter Server
    3. Unload platforms (remove button entities)
    4. Remove services (if this was the last instance)
    
    Args:
        hass: The Home Assistant instance
        entry: The config entry being unloaded
    
    Returns:
        True if unload was successful, False otherwise
    """
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})

    # -------------------------------------------------------------------------
    # Cancel the auto-sync timer
    # -------------------------------------------------------------------------
    if entry_data.get("unsub_timer"):
        entry_data["unsub_timer"]()
        _LOGGER.debug("Auto-sync timer cancelled")

    # -------------------------------------------------------------------------
    # Disconnect from the Matter Server
    # -------------------------------------------------------------------------
    coordinator = entry_data.get("coordinator")
    if coordinator:
        await coordinator.async_disconnect()

    # -------------------------------------------------------------------------
    # Unload platforms (button entities)
    # -------------------------------------------------------------------------
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # -------------------------------------------------------------------------
    # Clean up hass.data and remove services
    # -------------------------------------------------------------------------
    if unload_ok:
        # Remove this entry's data
        hass.data[DOMAIN].pop(entry.entry_id, None)

        # If no more entries exist, remove the services
        # (services should only exist while at least one entry is loaded)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SYNC_TIME)
            hass.services.async_remove(DOMAIN, SERVICE_SYNC_ALL)
            hass.services.async_remove(DOMAIN, SERVICE_REFRESH_DEVICES)

    return unload_ok
