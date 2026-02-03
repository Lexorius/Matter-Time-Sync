"""
Constants for Matter Time Sync integration.

This module defines all constants used throughout the integration:
- Domain name (used for service registration, data storage, etc.)
- Configuration keys (used in config flow and config entries)
- Default values for configuration options
- Service names
- Matter cluster IDs

These constants ensure consistency across all modules and make it
easy to change values in one place.

Author: @Loweack, @Lexorius, @miketth
Version: 2.0.3
"""

# =============================================================================
# INTEGRATION DOMAIN
# =============================================================================
# The domain is the unique identifier for this integration in Home Assistant.
# It's used for:
# - Service names (matter_time_sync.sync_time)
# - Config entry identification
# - Data storage in hass.data[DOMAIN]
# - Entity unique IDs
DOMAIN = "matter_time_sync"

# =============================================================================
# PLATFORMS
# =============================================================================
# List of Home Assistant platforms this integration provides.
# Currently only "button" - creates a sync button for each device.
# Could be extended to include "sensor" for last sync time, etc.
PLATFORMS = ["button"]

# =============================================================================
# CONFIGURATION KEYS
# =============================================================================
# These keys are used to store and retrieve configuration values
# from the config entry. They match the keys used in config_flow.py
# and the translation files.

# WebSocket URL for the Matter Server
# Example: "ws://core-matter-server:5580/ws" or "ws://192.168.1.100:5580/ws"
CONF_WS_URL = "ws_url"

# IANA timezone string
# Example: "Europe/Berlin", "America/New_York", "UTC"
CONF_TIMEZONE = "timezone"

# Comma-separated list of device name filters
# Example: "alpstuga, vindstyrka" - only sync devices containing these terms
# Empty string means all devices
CONF_DEVICE_FILTER = "device_filter"

# Whether automatic synchronization is enabled
# If True, devices are synced at regular intervals
CONF_AUTO_SYNC_ENABLED = "auto_sync_enabled"

# Interval for automatic synchronization (in minutes)
# Example: 60 = sync every hour
CONF_AUTO_SYNC_INTERVAL = "auto_sync_interval"

# Whether to only create buttons for devices with Time Sync cluster
# If True, devices without Time Sync support are skipped
# If False, buttons are created for all devices (sync may fail on some)
CONF_ONLY_TIME_SYNC_DEVICES = "only_time_sync_devices"

# =============================================================================
# DEFAULT VALUES
# =============================================================================
# Default values used when configuration options are not specified.
# These are also used as initial values in the config flow.

# Default WebSocket URL
# Note: This is for manual setup. Auto-detection from Matter integration
# uses "ws://core-matter-server:5580/ws" which works in Home Assistant OS.
DEFAULT_WS_URL = "ws://localhost:5580/ws"

# Default timezone (UTC is the safest default)
# The config flow auto-detects the Home Assistant timezone.
DEFAULT_TIMEZONE = "UTC"

# Default device filter (empty = all devices)
DEFAULT_DEVICE_FILTER = ""

# Auto-sync disabled by default (user must explicitly enable)
DEFAULT_AUTO_SYNC_ENABLED = False

# Default sync interval: 60 minutes (1 hour)
DEFAULT_AUTO_SYNC_INTERVAL = 60

# Only show devices with Time Sync cluster by default
# This avoids confusion from devices that can't actually be synced
DEFAULT_ONLY_TIME_SYNC_DEVICES = True

# =============================================================================
# AUTO-SYNC INTERVAL OPTIONS
# =============================================================================
# Available options for the auto-sync interval dropdown in the config flow.
# Values are in minutes.
# 
# Options:
# - 15 min: For devices that drift quickly
# - 30 min: Frequent sync
# - 1 hour: Default, good balance
# - 2 hours: Less frequent
# - 6 hours: For stable devices
# - 12 hours: Twice daily
# - 24 hours: Once daily
AUTO_SYNC_INTERVALS = [15, 30, 60, 120, 360, 720, 1440]

# =============================================================================
# MATTER CLUSTER IDS
# =============================================================================
# Matter uses numeric cluster IDs to identify functionality.
# The Time Synchronization cluster is 0x0038 (56 in decimal).
#
# This cluster provides:
# - SetUTCTime: Set the current UTC time
# - SetTimeZone: Set the timezone offset
# - SetDSTOffset: Set Daylight Saving Time offset
# - GetTimeGranularity: Query supported time precision
#
# Reference: Matter Application Cluster Specification, Chapter 11
TIME_SYNC_CLUSTER_ID = 0x0038  # 56 in decimal

# =============================================================================
# SERVICE NAMES
# =============================================================================
# Names for the services this integration provides.
# Full service names will be: matter_time_sync.sync_time, etc.

# Sync time on a single device
# Parameters: node_id (required), endpoint (optional, default 0)
SERVICE_SYNC_TIME = "sync_time"

# Sync time on all filtered devices
# Parameters: none
SERVICE_SYNC_ALL = "sync_all"

# Scan for new devices and create buttons
# Parameters: none
SERVICE_REFRESH_DEVICES = "refresh_devices"
