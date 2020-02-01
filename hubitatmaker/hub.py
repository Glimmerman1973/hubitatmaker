"""Hubitat API."""
from asyncio import gather
from functools import wraps
from logging import getLogger
import re
from typing import Any, Callable, Dict, List, Optional, Union, ValuesView, cast
from urllib.parse import quote, urlparse

import aiohttp
from bs4 import BeautifulSoup

from . import server
from .error import (
    InvalidAttribute,
    InvalidConfig,
    InvalidInfo,
    InvalidToken,
    RequestError,
)

_LOGGER = getLogger(__name__)

Listener = Callable[[], None]


class Hub:
    """A representation of a Hubitat hub.

    This class downloads initial device data from a Hubitat hub and waits for the 
    hub to push it state updates for devices. This means that the class must be
    able to receive update events. There are two ways to handle this: by relying on external code to pass in update events via this class's 
    """

    api_url: str
    app_id: str
    host: str
    scheme: str
    token: str

    def __init__(
        self,
        host: str,
        app_id: str,
        access_token: str,
        port: int = None,
        address: str = None,
    ):
        """Initialize a Hubitat hub interface.

        host:
          The URL of the host to connect to (e.g., http://10.0.1.99), or just
          the host name/address. If only a name or address are provided, http
          is assumed.
        app_id:
          The ID of the Maker API instance this interface should use
        access_token:
          The access token for the Maker API instance
        port:
          The port to listen on for events (optional). Defaults to a random open port.
        address:
          The address to listen on for events (optional). Defaults to 0.0.0.0.
        """
        if not host or not app_id or not access_token:
            raise InvalidConfig()

        host_url = urlparse(host)

        self.scheme = host_url.scheme or "http"
        self.host = host_url.netloc or host_url.path
        self.app_id = app_id
        self.token = access_token
        self.api_url = f"{self.scheme}://{self.host}/apps/api/{app_id}"
        self.address = address or "0.0.0.0"
        self.port = port or 0

        self._mac: Optional[str] = None
        self._started = False
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._info: Dict[str, str] = {}
        self._listeners: Dict[str, List[Listener]] = {}

        _LOGGER.info("Created hub %s", self)

    def __repr__(self) -> str:
        """Return a string representation of this hub."""
        return f"<Hub host={self.host} app_id={self.app_id}>"

    @property
    def devices(self) -> ValuesView[Dict[str, Any]]:
        """Return a list of devices managed by the Hubitat hub."""
        return self._devices.values()

    @property
    def hw_version(self) -> Optional[str]:
        """Return the Hubitat hub's hardware version."""
        return self._info.get("hw_version", "unknown")

    @property
    def id(self) -> str:
        """Return the unique ID of the Hubitat hub."""
        return f"{self.host}::{self.app_id}"

    @property
    def mac(self) -> str:
        """Return the MAC address of the Hubitat hub."""
        return self._info.get("mac", "unknown")

    @property
    def name(self) -> str:
        """Return the device name for the Hubitat hub."""
        return "Hubitat Elevation"

    @property
    def sw_version(self) -> str:
        """Return the Hubitat hub's software version."""
        return self._info.get("sw_version", "unknown")

    def add_device_listener(self, device_id: str, listener: Listener):
        """Listen for updates for a particular device."""
        if device_id not in self._listeners:
            self._listeners[device_id] = []
        self._listeners[device_id].append(listener)

    def remove_device_listeners(self, device_id: str):
        """Remove all listeners for a particular device."""
        self._listeners[device_id] = []

    def device_has_attribute(self, device_id: str, attr_name: str):
        """Return True if the given device has the given attribute."""
        state = self._devices[device_id]
        for attr in state["attributes"]:
            if attr["name"] == attr_name:
                return True
        return False

    async def check_config(self) -> None:
        """Verify that the hub is accessible.

        This method will raise a ConnectionError if there was a problem
        communicating with the hub.
        """
        try:
            await gather(self._load_info(), self._check_api())
        except aiohttp.ClientError as e:
            raise ConnectionError(str(e))

    async def start(self) -> None:
        """Download initial state data, and start an event server if requested.

        Hub and device data will not be available until this method has
        completed. Methods that rely on that data will raise an error if called
        before this method has completed.
        """
        try:
            self._server = server.create_server(
                self.process_event, self.address, self.port
            )
            self._server.start()
            await self.set_event_url(self._server.url)

            await gather(self._load_info(), self._load_devices())
            self._started = True
            _LOGGER.debug("Connected to Hubitat hub at %s", self.host)
        except aiohttp.ClientError as e:
            raise ConnectionError(str(e))

    def stop(self) -> None:
        """Remove all listeners and stop the event server (if running)."""
        if self._server:
            self._server.stop()
        self._listeners = {}
        self._started = False

    def get_device_attribute(
        self, device_id: str, attr_name: str
    ) -> Optional[Dict[str, Any]]:
        """Get an attribute value for a specific device."""
        state = self._devices.get(device_id)
        if state:
            for attr in state["attributes"]:
                if attr["name"] == attr_name:
                    return attr
        return None

    async def refresh_device(self, device_id: str):
        """Refresh a device's state."""
        await self._load_device(device_id, force_refresh=True)

    async def send_command(
        self, device_id: str, command: str, arg: Optional[Union[str, int]]
    ):
        """Send a device command to the hub."""
        path = f"devices/{device_id}/{command}"
        if arg:
            path += f"/{arg}"
        return await self._api_request(path)

    async def set_event_url(self, event_url: str):
        """Set the URL that Hubitat will POST device events to."""
        _LOGGER.info("Posting update to %s/postURL/%s", self.api_url, event_url)
        url = quote(str(event_url), safe="")
        await self._api_request(f"postURL/{url}")

    def process_event(self, event: Dict[str, Any]):
        """Process an event received from the hub."""
        content = event["content"]
        _LOGGER.debug(
            "received event for for %(displayName)s (%(deviceId)s) - %(name)s -> %(value)s",
            content,
        )
        device_id = content["deviceId"]
        self._update_device_attr(device_id, content["name"], content["value"])
        if device_id in self._listeners:
            for listener in self._listeners[device_id]:
                listener()

    async def _check_api(self):
        """Check for api access.

        An error will be raised if a test API request fails.
        """
        await self._api_request("devices")

    def _update_device_attr(
        self, device_id: str, attr_name: str, value: Union[int, str]
    ):
        """Update a device attribute value."""
        _LOGGER.debug("Updating %s of %s to %s", attr_name, device_id, value)
        try:
            state = self._devices[device_id]
        except KeyError:
            _LOGGER.warning("Tried to update unknown device %s", device_id)
            return

        for attr in state["attributes"]:
            if attr["name"] == attr_name:
                attr["currentValue"] = value
                return
        raise InvalidAttribute(f"Device {device_id} has no attribute {attr_name}")

    async def _load_info(self):
        """Load general info about the hub.

        This requires this hub to authenticate with the Hubitat hub if its
        security has been enabled.
        """
        url = f"{self.scheme}://{self.host}/hub/edit"
        _LOGGER.info("Getting hub info from %s...", url)
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.request("GET", url, timeout=timeout) as resp:
            if resp.status >= 400:
                _LOGGER.warning("Unable to access hub admin page: %s", resp.text)
            else:
                text = await resp.text()
                try:
                    soup = BeautifulSoup(text, "html.parser")
                    section = soup.find("h2", string="Hub Details")
                    self._info = _parse_details(section)
                    _LOGGER.debug("Loaded hub info: %s", self._info)
                except Exception as e:
                    _LOGGER.error("Error parsing hub info: %s", e)

    async def _load_devices(self, force_refresh=False):
        """Load the current state of all devices."""
        if force_refresh or len(self._devices) == 0:
            devices = await self._api_request("devices")
            _LOGGER.debug("Loaded device list")

            # load devices sequentially to avoid overloading the hub
            for dev in devices:
                await self._load_device(dev["id"], force_refresh)

    async def _load_device(self, device_id: str, force_refresh=False):
        """Return full info for a specific device.

        {
            "id": "1922",
            "name": "Generic Z-Wave Smart Dimmer",
            "label": "Bedroom Light",
            "attributes": [
                {
                    "dataType": "NUMBER",
                    "currentValue": 10,
                    "name": "level"
                },
                {
                    "values": ["on", "off"],
                    "name": "switch",
                    "currentValue": "on",
                    "dataType": "ENUM"
                }
            ],
            "capabilities": [
                "Switch",
                {"attributes": [{"name": "switch", "currentValue": "off", "dataType": "ENUM", "values": ["on", "off"]}]},
                "Configuration",
                "SwitchLevel"
                {"attributes": [{"name": "level", "dataType": null}]}
            ],
            "commands": [
                "configure",
                "flash",
                "off",
                "on",
                "refresh",
                "setLevel"
            ]
        ]
        """

        if force_refresh or device_id not in self._devices:
            _LOGGER.debug("Loading device %s", device_id)
            json = await self._api_request(f"devices/{device_id}")
            try:
                self._devices[device_id] = json
            except Exception as e:
                _LOGGER.error("Invalid device info: %s", json)
                raise e
            _LOGGER.debug("Loaded device %s", device_id)

    async def _api_request(self, path: str, method="GET"):
        """Make a Maker API request."""
        params = {"access_token": self.token}
        async with aiohttp.request(
            method, f"{self.api_url}/{path}", params=params
        ) as resp:
            if resp.status >= 400:
                if resp.status == 401:
                    raise InvalidToken()
                else:
                    raise RequestError(resp)
            json = await resp.json()
            if "error" in json and json["error"]:
                raise RequestError(resp)
            return json


_DETAILS_MAPPING = {
    "Hubitat Elevation® Platform Version": "sw_version",
    "Hardware Version": "hw_version",
    "Hub UID": "uid",
    "IP Address": "address",
    "MAC Address": "mac",
}


def _parse_details(tag):
    """Parse hub details from HTML."""
    details: Dict[str, str] = {}
    group = tag.find_next_sibling("div")
    while group is not None:
        heading = group.find("div", class_="menu-header").text.strip()
        content = group.find("div", class_="menu-text").text.strip()
        if heading in _DETAILS_MAPPING:
            details[_DETAILS_MAPPING[heading]] = content
        group = group.find_next_sibling("div")
    return details
