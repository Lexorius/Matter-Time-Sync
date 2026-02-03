"""
Coordinator for Matter Time Sync.

This module handles all communication with the Matter Server via WebSocket.
It provides methods to:
- Connect to and disconnect from the Matter Server
- Retrieve all Matter nodes (devices)
- Synchronize time on individual devices or all devices

The coordinator manages:
- WebSocket connection lifecycle (connect, disconnect, reconnect)
- Connection testing and automatic reconnection
- Command sending with retry logic
- Node/device parsing and caching
- Time synchronization using the Matter Time Sync cluster (0x0038)

Time Synchronization Protocol:
------------------------------
Matter devices that support the Time Synchronization cluster can receive
time updates via three commands:

1. SetTimeZone: Sets the UTC offset (e.g., +3600 for UTC+1)
2. SetDSTOffset: Sets the Daylight Saving Time offset (we set this to 0)
3. SetUTCTime: Sets the current UTC time in microseconds since CHIP epoch

The CHIP epoch is January 1, 2000 00:00:00 UTC (different from Unix epoch 1970).

Author: @Loweack, @Lexorius, @miketth
Version: 2.0.3 (with connection retry fix)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import WSMsgType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_TIMEZONE,
    CONF_WS_URL,
    DEFAULT_TIMEZONE,
    TIME_SYNC_CLUSTER_ID,
)

_LOGGER = logging.getLogger(__name__)

# =============================================================================
# CHIP/MATTER EPOCH DEFINITION
# =============================================================================
# Matter/CHIP uses a different epoch than Unix. The CHIP epoch starts at
# January 1, 2000 00:00:00 UTC. All timestamps in the Time Synchronization
# cluster are expressed as microseconds since this epoch.
#
# Unix epoch: 1970-01-01 00:00:00 UTC
# CHIP epoch: 2000-01-01 00:00:00 UTC
# Difference: 946684800 seconds (30 years)
_CHIP_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# =============================================================================
# CONNECTION SETTINGS
# =============================================================================
# These constants control the connection behavior and retry logic
MAX_RETRIES = 3              # Number of connection/command retry attempts
RETRY_DELAY_SECONDS = 2      # Delay between retry attempts
CONNECTION_TIMEOUT = 10      # Timeout for WebSocket connection (seconds)
COMMAND_TIMEOUT = 10         # Timeout waiting for command response (seconds)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================
def _to_chip_epoch_us(dt: datetime) -> int:
    """
    Convert a datetime to microseconds since CHIP epoch (2000-01-01).
    
    The Matter Time Synchronization cluster uses timestamps in microseconds
    since the CHIP epoch (January 1, 2000 00:00:00 UTC).
    
    Example:
        2025-02-03 15:30:00 UTC -> 788887800000000 microseconds since 2000-01-01
    
    Args:
        dt: A datetime object (can be in any timezone, will be converted to UTC)
    
    Returns:
        Integer representing microseconds since CHIP epoch
    """
    # Convert to UTC to ensure consistent calculation
    dt_utc = dt.astimezone(timezone.utc)
    # Calculate the difference from CHIP epoch
    delta = dt_utc - _CHIP_EPOCH
    # Convert to microseconds (seconds * 1,000,000)
    return int(delta.total_seconds() * 1_000_000)


# =============================================================================
# COORDINATOR CLASS
# =============================================================================
class MatterTimeSyncCoordinator:
    """
    Coordinator to manage Matter Server WebSocket connection.
    
    This class is responsible for:
    - Maintaining a WebSocket connection to the Matter Server
    - Sending commands and receiving responses
    - Parsing Matter node information
    - Synchronizing time on devices
    
    The coordinator uses several locks to ensure thread safety:
    - _lock: Protects connection state changes
    - _command_lock: Ensures only one command is in-flight at a time
    - _per_node_sync_locks: Prevents concurrent syncs on the same device
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """
        Initialize the coordinator.
        
        Args:
            hass: The Home Assistant instance
            entry: The config entry containing user configuration
        """
        self.hass = hass
        self.entry = entry
        
        # Configuration from config entry
        self._ws_url = entry.data.get(CONF_WS_URL, "ws://localhost:5580/ws")
        self._timezone = entry.data.get(CONF_TIMEZONE, DEFAULT_TIMEZONE)
        
        # WebSocket connection state
        self._session: aiohttp.ClientSession | None = None  # HTTP session for WebSocket
        self._ws: aiohttp.ClientWebSocketResponse | None = None  # WebSocket connection
        self._message_id = 0  # Incrementing ID for each message sent
        self._connected = False  # Are we connected?
        
        # Node cache - stores parsed node information to avoid repeated queries
        self._nodes_cache: list[dict[str, Any]] = []
        
        # Locks for thread safety
        self._lock = asyncio.Lock()  # Protects connection state
        self._command_lock = asyncio.Lock()  # One command at a time
        
        # Per-node locks prevent multiple concurrent syncs on the same device
        # This avoids race conditions when pressing the sync button rapidly
        self._per_node_sync_locks: dict[int, asyncio.Lock] = {}

    # =========================================================================
    # CONNECTION PROPERTIES
    # =========================================================================
    @property
    def is_connected(self) -> bool:
        """
        Check if we have an active connection to the Matter Server.
        
        Returns:
            True if connected and WebSocket is open, False otherwise
        """
        return self._connected and self._ws is not None and not self._ws.closed

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================
    async def async_connect(self) -> bool:
        """
        Connect to the Matter Server WebSocket.
        
        Establishes a new WebSocket connection to the Matter Server.
        If already connected, returns True immediately.
        
        Returns:
            True if connection successful, False otherwise
        """
        async with self._lock:
            # Already connected? Nothing to do.
            if self.is_connected:
                return True

            try:
                # Clean up any stale connection first
                await self._cleanup_connection_internal()

                # Create a new HTTP session and WebSocket connection
                self._session = aiohttp.ClientSession()
                self._ws = await self._session.ws_connect(
                    self._ws_url, 
                    timeout=aiohttp.ClientTimeout(total=CONNECTION_TIMEOUT)
                )
                self._connected = True
                _LOGGER.info("Connected to Matter Server at %s", self._ws_url)
                return True
                
            except asyncio.TimeoutError:
                _LOGGER.error("Timeout connecting to Matter Server at %s", self._ws_url)
                await self._cleanup_connection_internal()
                return False
            except aiohttp.ClientError as err:
                _LOGGER.error("Client error connecting to Matter Server: %s", err)
                await self._cleanup_connection_internal()
                return False
            except Exception as err:
                _LOGGER.error("Failed to connect to Matter Server: %s", err)
                await self._cleanup_connection_internal()
                return False

    async def _cleanup_connection_internal(self) -> None:
        """
        Close and cleanup WebSocket and session (internal, no lock).
        
        This is the internal cleanup method that doesn't acquire the lock.
        Used by methods that already hold the lock.
        """
        self._connected = False
        
        # Close WebSocket if open
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass  # Ignore errors during cleanup
            self._ws = None
            
        # Close HTTP session if open
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass  # Ignore errors during cleanup
            self._session = None

    async def _cleanup_connection(self) -> None:
        """
        Close and cleanup WebSocket and session (with lock).
        
        Public cleanup method that acquires the lock for thread safety.
        """
        async with self._lock:
            await self._cleanup_connection_internal()

    async def async_disconnect(self) -> None:
        """
        Disconnect from the Matter Server.
        
        Cleanly closes the WebSocket connection and HTTP session.
        """
        await self._cleanup_connection()
        _LOGGER.debug("Disconnected from Matter Server")

    async def async_test_connection(self) -> bool:
        """
        Test if the connection is alive by sending a simple command.
        
        Sends a "get_nodes" command to verify the WebSocket is working.
        This is useful to detect stale connections that appear open but
        are actually dead.
        
        Returns:
            True if the connection is working, False otherwise
        """
        if not self.is_connected:
            return False

        try:
            # Send a simple command to test the connection
            response = await self._async_send_command_internal("get_nodes", timeout=5)
            return response is not None
        except Exception as err:
            _LOGGER.debug("Connection test failed: %s", err)
            return False

    async def async_ensure_connected(self) -> bool:
        """
        Ensure we have a working connection, reconnect if necessary.
        
        This method:
        1. Checks if we think we're connected
        2. Tests the connection with a simple command
        3. Reconnects if the test fails
        4. Retries up to MAX_RETRIES times
        
        Returns:
            True if we have a working connection, False if all retries failed
        """
        # First check if we appear to be connected
        if self.is_connected:
            # Test if the connection actually works
            if await self.async_test_connection():
                return True
            else:
                _LOGGER.warning("Connection test failed, reconnecting...")
                await self._cleanup_connection()

        # Try to connect with retries
        for attempt in range(MAX_RETRIES):
            _LOGGER.debug("Connection attempt %d/%d", attempt + 1, MAX_RETRIES)
            
            if await self.async_connect():
                # Verify the new connection works
                if await self.async_test_connection():
                    _LOGGER.info("Successfully connected to Matter Server")
                    return True
                else:
                    _LOGGER.warning("Connected but connection test failed")
                    await self._cleanup_connection()

            # Wait before retrying (except on last attempt)
            if attempt < MAX_RETRIES - 1:
                _LOGGER.debug("Waiting %d seconds before retry...", RETRY_DELAY_SECONDS)
                await asyncio.sleep(RETRY_DELAY_SECONDS)

        _LOGGER.error(
            "Failed to connect to Matter Server after %d attempts", MAX_RETRIES
        )
        return False

    # =========================================================================
    # COMMAND SENDING
    # =========================================================================
    async def _async_send_command_internal(
        self, 
        command: str, 
        args: dict[str, Any] | None = None, 
        timeout: int = COMMAND_TIMEOUT
    ) -> dict[str, Any] | None:
        """
        Send a command to the Matter Server (internal, assumes connected).
        
        This is the low-level command sender that doesn't handle reconnection.
        It sends a JSON message and waits for a response with matching message_id.
        
        Matter Server Protocol:
        - Request: {"message_id": "1", "command": "get_nodes", "args": {...}}
        - Response: {"message_id": "1", "result": [...]} or {"message_id": "1", "error_code": ...}
        
        Args:
            command: The command name (e.g., "get_nodes", "device_command")
            args: Optional arguments for the command
            timeout: Timeout in seconds waiting for response
        
        Returns:
            The response dictionary, or None if failed
        """
        if not self._ws or self._ws.closed:
            return None

        # Generate a unique message ID for this request
        self._message_id += 1
        message_id = str(self._message_id)

        # Build the request message
        request: dict[str, Any] = {
            "message_id": message_id,
            "command": command,
        }
        if args:
            request["args"] = args

        try:
            # Send the request as JSON
            await self._ws.send_json(request)

            # Wait for the response with matching message_id
            async def _wait_for_response() -> dict[str, Any] | None:
                async for msg in self._ws:
                    if msg.type == WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        # Check if this is our response
                        if data.get("message_id") == message_id:
                            # Check for error
                            if "error_code" in data:
                                _LOGGER.debug(
                                    "Matter Server error for command %s: %s",
                                    command,
                                    data.get("details", "Unknown error"),
                                )
                                return None
                            return data
                    elif msg.type == WSMsgType.ERROR:
                        _LOGGER.error("WebSocket error: %s", msg.data)
                        return None
                    elif msg.type == WSMsgType.CLOSED:
                        _LOGGER.warning("WebSocket closed unexpectedly")
                        return None
                return None

            return await asyncio.wait_for(_wait_for_response(), timeout=timeout)

        except asyncio.TimeoutError:
            _LOGGER.error("Timeout waiting for response to %s", command)
            return None
        except Exception as err:
            _LOGGER.error("Error sending command to Matter Server: %s", err)
            return None

    async def _async_send_command(
        self, 
        command: str, 
        args: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """
        Send a command to the Matter Server with retry logic.
        
        This is the public command sender that handles reconnection and retries.
        It uses _command_lock to ensure only one command is in-flight at a time.
        
        Args:
            command: The command name
            args: Optional arguments for the command
        
        Returns:
            The response dictionary, or None if all retries failed
        """
        async with self._command_lock:
            for attempt in range(MAX_RETRIES):
                # Ensure we're connected
                if not self.is_connected:
                    if not await self.async_connect():
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY_SECONDS)
                            continue
                        return None

                # Send the command
                result = await self._async_send_command_internal(command, args)
                
                if result is not None:
                    return result

                # Command failed - check if it's a connection issue
                if not self.is_connected or (self._ws and self._ws.closed):
                    _LOGGER.warning(
                        "Connection lost during command %s, attempt %d/%d",
                        command, attempt + 1, MAX_RETRIES
                    )
                    await self._cleanup_connection()
                    
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        continue
                else:
                    # Command failed but connection is OK - don't retry
                    break

            return None

    # =========================================================================
    # DEVICE/NODE MANAGEMENT
    # =========================================================================
    def _get_ha_device_name(self, node_id: int) -> str | None:
        """
        Try to get the device name from Home Assistant's device registry.
        
        This allows us to use the user-defined name if the device was renamed
        in Home Assistant, which provides a better user experience.
        
        The Matter integration uses identifiers like:
        - ("matter", "7")
        - ("matter", "deviceid_7")
        - ("matter", "xxx_7")
        
        Args:
            node_id: The Matter node ID
        
        Returns:
            The device name from HA, or None if not found
        """
        try:
            device_reg = dr.async_get(self.hass)
            for device in device_reg.devices.values():
                for identifier in device.identifiers:
                    # Only look at Matter devices
                    if identifier[0] != "matter":
                        continue

                    # Check if this identifier matches our node_id
                    id_str = str(identifier[1])
                    if (
                        id_str == str(node_id)
                        or id_str == f"deviceid_{node_id}"
                        or id_str.endswith(f"_{node_id}")
                    ):
                        # Prefer user-defined name over default name
                        if device.name_by_user:
                            _LOGGER.debug(
                                "Found HA device name for node %s: %s (user-defined)",
                                node_id,
                                device.name_by_user,
                            )
                            return device.name_by_user
                        if device.name:
                            _LOGGER.debug(
                                "Found HA device name for node %s: %s", 
                                node_id, 
                                device.name
                            )
                            return device.name
        except Exception as err:
            _LOGGER.debug("Could not get HA device name: %s", err)
        return None

    async def async_get_matter_nodes(self) -> list[dict[str, Any]]:
        """
        Get all Matter nodes from the server.
        
        Sends a "get_nodes" command to the Matter Server and parses
        the response into a list of node dictionaries.
        
        Returns:
            List of parsed node dictionaries, or cached list if query fails
        """
        # Ensure we're connected first
        if not await self.async_ensure_connected():
            _LOGGER.error("Cannot get Matter nodes: not connected")
            return self._nodes_cache

        # Send the get_nodes command
        response = await self._async_send_command("get_nodes")
        if not response:
            _LOGGER.warning("Failed to get nodes from Matter Server, using cache")
            return self._nodes_cache

        # Parse the raw nodes into our format
        raw_nodes = response.get("result", [])
        self._nodes_cache = self._parse_nodes(raw_nodes)
        return self._nodes_cache

    def _get_time_sync_endpoints(self, attributes: dict[str, Any]) -> list[int]:
        """
        Find endpoints that expose the Time Synchronization cluster (56).
        
        Matter devices have multiple "endpoints", each with multiple "clusters".
        The Time Sync cluster (0x0038 = 56) can be on any endpoint, though
        it's usually on endpoint 0.
        
        Attributes are keyed as "endpoint/cluster/attribute", e.g., "0/56/0".
        
        Args:
            attributes: The raw attributes dictionary from the Matter Server
        
        Returns:
            Sorted list of endpoint IDs that have the Time Sync cluster
        """
        endpoints: set[int] = set()
        for key in attributes.keys():
            parts = key.split("/")
            if len(parts) < 2:
                continue
            try:
                endpoint_id = int(parts[0])
                cluster_id = int(parts[1])
            except ValueError:
                continue
            # 56 = Time Synchronization cluster
            if cluster_id == 56:
                endpoints.add(endpoint_id)
        return sorted(endpoints)

    def _parse_nodes(self, raw_nodes: list) -> list[dict[str, Any]]:
        """
        Parse raw node data from Matter Server into a usable format.
        
        Extracts device information, determines if the device supports
        Time Sync, and selects the best name for the device.
        
        Matter Server returns nodes with attributes in the format:
        "endpoint/cluster/attribute" -> value
        
        Common attributes (on endpoint 0, cluster 40 = Basic Information):
        - 0/40/1: VendorName
        - 0/40/3: ProductName
        - 0/40/5: NodeLabel (user-defined name on the device)
        - 0/40/15: SerialNumber
        
        Args:
            raw_nodes: List of raw node dictionaries from Matter Server
        
        Returns:
            List of parsed node dictionaries with standardized fields
        """
        parsed: list[dict[str, Any]] = []
        
        for node in raw_nodes:
            node_id = node.get("node_id")
            if node_id is None:
                continue

            attributes = node.get("attributes", {})

            # Extract device information from Basic Information cluster (0/40/*)
            device_info = {
                "vendor_name": attributes.get("0/40/1", "Unknown"),
                "product_name": attributes.get("0/40/3", ""),
                "node_label": attributes.get("0/40/5", ""),
                "serial_number": attributes.get("0/40/15", ""),
            }

            # Check if this device supports Time Sync
            time_sync_endpoints = self._get_time_sync_endpoints(attributes)
            has_time_sync = bool(time_sync_endpoints)

            # Determine the best name for this device
            # Priority: HA user name > NodeLabel > ProductName > fallback
            ha_name = self._get_ha_device_name(node_id)
            node_label = device_info.get("node_label", "")
            product_name = device_info.get("product_name", "")

            if ha_name:
                name = ha_name
                name_source = "home_assistant"
            elif node_label:
                name = node_label
                name_source = "node_label"
            elif product_name:
                name = product_name
                name_source = "product_name"
            else:
                name = f"Matter Node {node_id}"
                name_source = "fallback"

            _LOGGER.debug(
                "Node %s: name='%s' (source: %s), product='%s', has_time_sync=%s",
                node_id,
                name,
                name_source,
                product_name,
                has_time_sync,
            )

            parsed.append({
                "node_id": node_id,
                "name": name,
                "name_source": name_source,
                "product_name": product_name,
                "device_info": device_info,
                "has_time_sync": has_time_sync,
                "time_sync_endpoints": time_sync_endpoints,
            })

        _LOGGER.info("Parsed %d Matter nodes", len(parsed))
        return parsed

    def _check_time_sync_cluster(self, attributes: dict) -> bool:
        """
        Check if a node has the Time Synchronization cluster.
        
        Simple helper that returns True if any attribute key indicates
        the presence of cluster 56 (Time Sync).
        
        Args:
            attributes: The raw attributes dictionary
        
        Returns:
            True if Time Sync cluster is present, False otherwise
        """
        for key in attributes.keys():
            parts = key.split("/")
            if len(parts) >= 2:
                try:
                    cluster = int(parts[1])
                    if cluster == 56:  # Time Sync cluster
                        return True
                except ValueError:
                    continue
        return False

    # =========================================================================
    # TIME SYNCHRONIZATION
    # =========================================================================
    async def async_sync_time(self, node_id: int, endpoint: int = 0) -> bool:
        """
        Sync time on a Matter device.
        
        This is the main public method for syncing time on a single device.
        It uses a per-node lock to prevent concurrent syncs on the same device
        (which can happen if the user clicks the button rapidly).
        
        The sync process:
        1. Ensure we're connected to the Matter Server
        2. Determine the correct endpoint (auto-detect if needed)
        3. Calculate current time and timezone offset
        4. Send SetTimeZone command
        5. Send SetDSTOffset command (always 0)
        6. Send SetUTCTime command
        
        Args:
            node_id: The Matter node ID of the device
            endpoint: The endpoint to use (0 = auto-detect)
        
        Returns:
            True if sync was successful, False otherwise
        """
        # Get or create a lock for this specific node
        lock = self._per_node_sync_locks.setdefault(node_id, asyncio.Lock())
        
        async with lock:
            # Ensure connection before syncing
            if not await self.async_ensure_connected():
                _LOGGER.error("Cannot sync time for node %s: not connected", node_id)
                return False

            # Ensure we have node info for endpoint auto-selection
            if not self._nodes_cache:
                await self.async_get_matter_nodes()

            # ----------------------------------------------------------------
            # Determine the correct endpoint
            # ----------------------------------------------------------------
            # Most devices have Time Sync on endpoint 0, but some may have it
            # on a different endpoint. If the user specified endpoint 0 and
            # it's not available, try to auto-detect.
            endpoint_id = endpoint
            if endpoint == 0:
                node = next(
                    (n for n in self._nodes_cache if n.get("node_id") == node_id),
                    None,
                )
                endpoints = (node or {}).get("time_sync_endpoints") or []
                if endpoints and 0 not in endpoints:
                    endpoint_id = endpoints[0]
                    _LOGGER.debug(
                        "Using Time Sync endpoint %s for node %s (cluster 56 not on endpoint 0)",
                        endpoint_id,
                        node_id,
                    )

            # ----------------------------------------------------------------
            # Calculate time and timezone information
            # ----------------------------------------------------------------
            # Get the configured timezone
            try:
                tz = ZoneInfo(self._timezone)
            except Exception:
                _LOGGER.warning("Invalid timezone %s, using UTC", self._timezone)
                tz = ZoneInfo("UTC")

            # Get current time in local timezone and UTC
            now = datetime.now(tz)
            utc_now = now.astimezone(ZoneInfo("UTC"))

            # Calculate UTC offset in seconds
            # This includes DST when applicable (e.g., Europe/Berlin = +3600 in winter, +7200 in summer)
            total_offset = int(now.utcoffset().total_seconds()) if now.utcoffset() else 0

            # IMPORTANT: We merge DST into the UTC offset and set DST to 0
            # This avoids issues with devices that don't handle DST correctly
            utc_offset = total_offset
            dst_offset = 0

            # Convert current UTC time to CHIP epoch microseconds
            utc_microseconds = _to_chip_epoch_us(utc_now)

            _LOGGER.info(
                "Syncing time for node %s: local=%s, UTC=%s, offset=%ds (%+.1fh), DST=%ds (forced to 0)",
                node_id,
                now.isoformat(),
                utc_now.isoformat(),
                utc_offset,
                utc_offset / 3600,
                dst_offset,
            )

            # ================================================================
            # SYNC SEQUENCE
            # ================================================================
            # We send commands in this order to minimize the time the device
            # displays incorrect time:
            # 1. SetTimeZone - Set the offset first
            # 2. SetDSTOffset - Set DST (always 0)
            # 3. SetUTCTime - Set the actual time last

            # ----------------------------------------------------------------
            # STEP 1: Set TimeZone
            # ----------------------------------------------------------------
            # TimeZone is a list of timezone entries. Each entry has:
            # - offset: UTC offset in seconds (e.g., 3600 for UTC+1)
            # - validAt: When this timezone becomes valid (0 = always)
            # - name: Optional IANA timezone name (e.g., "Europe/Berlin")
            tz_list_with_name = [
                {"offset": utc_offset, "validAt": 0, "name": self._timezone}
            ]
            tz_list_no_name = [{"offset": utc_offset, "validAt": 0}]

            # Try PascalCase key first (Matter spec), then camelCase (some servers)
            tz_response = await self._async_send_command(
                "device_command",
                {
                    "node_id": node_id,
                    "endpoint_id": endpoint_id,
                    "cluster_id": TIME_SYNC_CLUSTER_ID,
                    "command_name": "SetTimeZone",
                    "payload": {"TimeZone": tz_list_with_name},
                },
            )
            if not tz_response:
                tz_response = await self._async_send_command(
                    "device_command",
                    {
                        "node_id": node_id,
                        "endpoint_id": endpoint_id,
                        "cluster_id": TIME_SYNC_CLUSTER_ID,
                        "command_name": "SetTimeZone",
                        "payload": {"timeZone": tz_list_with_name},
                    },
                )

            if tz_response:
                _LOGGER.debug(
                    "SetTimeZone successful for node %s (offset=%d)", node_id, utc_offset
                )
            else:
                # Try without the timezone name (some devices don't support it)
                _LOGGER.warning(
                    "SetTimeZone failed for node %s, trying without name", node_id
                )

                tz_response = await self._async_send_command(
                    "device_command",
                    {
                        "node_id": node_id,
                        "endpoint_id": endpoint_id,
                        "cluster_id": TIME_SYNC_CLUSTER_ID,
                        "command_name": "SetTimeZone",
                        "payload": {"TimeZone": tz_list_no_name},
                    },
                )
                if not tz_response:
                    tz_response = await self._async_send_command(
                        "device_command",
                        {
                            "node_id": node_id,
                            "endpoint_id": endpoint_id,
                            "cluster_id": TIME_SYNC_CLUSTER_ID,
                            "command_name": "SetTimeZone",
                            "payload": {"timeZone": tz_list_no_name},
                        },
                    )

                if tz_response:
                    _LOGGER.debug("SetTimeZone (without name) successful for node %s", node_id)
                else:
                    _LOGGER.warning("SetTimeZone completely failed for node %s", node_id)

            # ----------------------------------------------------------------
            # STEP 2: Set DST Offset
            # ----------------------------------------------------------------
            # DSTOffset is a list of DST entries. Each entry has:
            # - offset: DST offset in seconds (we always use 0)
            # - validStarting: When this DST period starts (CHIP epoch us)
            # - validUntil: When this DST period ends (CHIP epoch us)
            far_future_us = _to_chip_epoch_us(utc_now + timedelta(days=365))

            dst_list = [
                {
                    "offset": dst_offset,  # Always 0 - we merge DST into UTC offset
                    "validStarting": 0,
                    "validUntil": far_future_us,  # Valid for 1 year
                }
            ]

            # Try PascalCase first, then camelCase
            dst_response = await self._async_send_command(
                "device_command",
                {
                    "node_id": node_id,
                    "endpoint_id": endpoint_id,
                    "cluster_id": TIME_SYNC_CLUSTER_ID,
                    "command_name": "SetDSTOffset",
                    "payload": {"DSTOffset": dst_list},
                },
            )

            if not dst_response:
                _LOGGER.debug("SetDSTOffset (PascalCase) failed, trying camelCase...")
                dst_response = await self._async_send_command(
                    "device_command",
                    {
                        "node_id": node_id,
                        "endpoint_id": endpoint_id,
                        "cluster_id": TIME_SYNC_CLUSTER_ID,
                        "command_name": "SetDSTOffset",
                        "payload": {"dstOffset": dst_list},
                    },
                )

            if dst_response:
                _LOGGER.debug("SetDSTOffset (0) successful for node %s", node_id)
            else:
                # DST command is optional - many devices don't support it
                _LOGGER.debug(
                    "SetDSTOffset not supported or failed for node %s (this is often OK)",
                    node_id,
                )

            # ----------------------------------------------------------------
            # STEP 3: Set UTC Time (LAST - most important!)
            # ----------------------------------------------------------------
            # UTCTime command parameters:
            # - UTCTime: Microseconds since CHIP epoch (2000-01-01)
            # - granularity: Time granularity (3 = milliseconds)
            payload_utc_pascal = {"UTCTime": utc_microseconds, "granularity": 3}
            payload_utc_camel = {"utcTime": utc_microseconds, "granularity": 3}

            # Try PascalCase first
            time_response = await self._async_send_command(
                "device_command",
                {
                    "node_id": node_id,
                    "endpoint_id": endpoint_id,
                    "cluster_id": TIME_SYNC_CLUSTER_ID,
                    "command_name": "SetUTCTime",
                    "payload": payload_utc_pascal,
                },
            )

            if not time_response:
                _LOGGER.debug("SetUTCTime (PascalCase) failed, trying camelCase...")
                time_response = await self._async_send_command(
                    "device_command",
                    {
                        "node_id": node_id,
                        "endpoint_id": endpoint_id,
                        "cluster_id": TIME_SYNC_CLUSTER_ID,
                        "command_name": "SetUTCTime",
                        "payload": payload_utc_camel,
                    },
                )

            if not time_response:
                _LOGGER.error(
                    "Failed to set UTC time for node %s (tried both formats)", node_id
                )
                return False

            _LOGGER.debug("SetUTCTime successful for node %s", node_id)

            # ----------------------------------------------------------------
            # SUCCESS!
            # ----------------------------------------------------------------
            _LOGGER.info(
                "Time synced for node %s: %s (UTC offset: %+ds = %+.1fh, DST: %d)",
                node_id,
                now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                utc_offset,
                utc_offset / 3600,
                dst_offset,
            )
            return True

    async def async_sync_all_devices(self) -> None:
        """
        Sync time on all filtered devices.
        
        This method syncs time on all Matter devices that:
        1. Match the device name filter (if configured)
        2. Have the Time Sync cluster (if "only_time_sync_devices" is enabled)
        
        It logs a summary at the end showing how many devices were synced,
        failed, or skipped.
        """
        # Ensure connection before syncing
        if not await self.async_ensure_connected():
            _LOGGER.error("Cannot sync all devices: not connected to Matter Server")
            return

        # Get all nodes from the Matter Server
        nodes = await self.async_get_matter_nodes()

        # Get filter settings from config
        device_filters = self.entry.data.get("device_filter", "")
        device_filters = [t.strip().lower() for t in device_filters.split(",") if t.strip()]
        only_time_sync = self.entry.data.get("only_time_sync_devices", True)

        # Counters for summary
        count = 0      # Successfully synced
        skipped = 0    # Skipped due to filters
        failed = 0     # Sync failed
        
        for node in nodes:
            node_id = node.get("node_id")
            node_name = node.get("name", f"Node {node_id}")
            has_time_sync = node.get("has_time_sync", False)

            # Skip devices without Time Sync cluster (if option enabled)
            if only_time_sync and not has_time_sync:
                skipped += 1
                continue

            # Skip devices that don't match the name filter
            if device_filters and not any(term in node_name.lower() for term in device_filters):
                skipped += 1
                continue

            _LOGGER.info("Auto-syncing node %s (%s)", node_id, node_name)
            
            # Re-check connection before each sync
            if not self.is_connected:
                if not await self.async_ensure_connected():
                    _LOGGER.error("Lost connection during sync_all, aborting")
                    failed += 1
                    break
            
            # Sync this device
            success = await self.async_sync_time(node_id)
            if success:
                count += 1
            else:
                failed += 1

        _LOGGER.info(
            "Sync all completed: %d synced, %d failed, %d skipped", 
            count, failed, skipped
        )

    def update_config(self, ws_url: str, timezone: str) -> None:
        """
        Update configuration (called when options change).
        
        This allows updating the WebSocket URL and timezone without
        recreating the coordinator.
        
        Args:
            ws_url: New WebSocket URL
            timezone: New timezone string
        """
        self._ws_url = ws_url
        self._timezone = timezone
        _LOGGER.debug("Configuration updated: URL=%s, TZ=%s", ws_url, timezone)
