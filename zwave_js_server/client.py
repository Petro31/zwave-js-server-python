"""Client."""
import asyncio
import logging
import pprint
import random
import uuid
from typing import Any, Awaitable, Callable, List, Optional, cast, Dict

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType, client_exceptions

from .event import Event
from .model.driver import Driver

STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"


async def gather_callbacks(
    logger: logging.Logger, name: str, callbacks: List[Callable[[], Awaitable[None]]]
) -> None:
    """Gather callbacks."""
    results = await asyncio.gather(*[cb() for cb in callbacks], return_exceptions=True)
    for result, callback in zip(results, callbacks):
        if not isinstance(result, Exception):
            continue
        logger.error("Unexpected error in %s %s", name, callback, exc_info=result)


class NotConnected(Exception):
    """Exception raised when trying to handle unknown handler."""


class InvalidState(Exception):
    """Exception raised when data gets in invalid state."""


class FailedCommand(Exception):
    """When a command has failed."""

    def __init__(self, message_id: str, error_code: str):
        """Initialize a failed command error."""
        super().__init__(f"Command failed: {error_code}")
        self.message_id = message_id
        self.error_code = error_code


class Client:
    """Class to manage the IoT connection."""

    def __init__(self, ws_server_url: str, aiohttp_session: ClientSession):
        """Initialize the Client class."""
        self.ws_server_url = ws_server_url
        self.aiohttp_session = aiohttp_session
        self.driver: Optional[Driver] = None
        # The WebSocket client
        self.client: Optional[ClientWebSocketResponse] = None
        # Scheduled sleep task till next connection retry
        self.retry_task: Optional[asyncio.Task] = None
        # Boolean to indicate if we wanted the connection to close
        self.close_requested = False
        # The current number of attempts to connect, impacts wait time
        self.tries = 0
        # Current state of the connection
        self.state = STATE_DISCONNECTED
        self._on_connect: List[Callable[[], Awaitable[None]]] = []
        self._on_disconnect: List[Callable[[], Awaitable[None]]] = []
        self._on_initialized: List[Callable[[], Awaitable[None]]] = []
        self._logger = logging.getLogger(__package__)
        self._disconnect_event: Optional[asyncio.Event] = None

    def async_handle_message(self, msg: dict) -> None:
        """Handle incoming message.

        Run all async tasks in a wrapper to log appropriately.
        """
        # Right now only result is from receiving state.
        if msg["type"] == "result":
            if self.driver is None:
                self.driver = Driver(self, msg["result"]["state"])
                self._logger.info(
                    "Z-Wave JS initialized. %s nodes", len(self.driver.controller.nodes)
                )
                asyncio.create_task(
                    gather_callbacks(
                        self._logger, "on_initialized", self._on_initialized
                    )
                )
            else:
                # TODO how do we handle reconnect?
                pass

            return

        if self.driver is None:
            raise InvalidState("Did not receive state as first message")

        if msg["type"] != "event":
            # Can't handle
            return

        event = Event(type=msg["event"]["event"], data=msg["event"])
        self.driver.receive_event(event)

    def register_on_connect(
        self, on_connect_cb: Callable[[], Awaitable[None]]
    ) -> Callable:
        """Register an async on_connect callback."""

        def unsubscribe() -> None:
            """Unsubscribe listeners."""
            if on_connect_cb in self._on_connect:
                self._on_connect.remove(on_connect_cb)

        self._on_connect.append(on_connect_cb)
        return unsubscribe

    def register_on_disconnect(
        self, on_disconnect_cb: Callable[[], Awaitable[None]]
    ) -> Callable:
        """Register an async on_disconnect callback."""

        def unsubscribe() -> None:
            """Unsubscribe listeners."""
            if on_disconnect_cb in self._on_disconnect:
                self._on_disconnect.remove(on_disconnect_cb)

        self._on_disconnect.append(on_disconnect_cb)
        return unsubscribe

    def register_on_initialized(
        self, on_initialized_cb: Callable[[], Awaitable[None]]
    ) -> Callable:
        """Register an async on_initialized_cb callback."""

        def unsubscribe() -> None:
            """Unsubscribe listeners."""
            if on_initialized_cb in self._on_initialized:
                self._on_initialized.remove(on_initialized_cb)

        self._on_initialized.append(on_initialized_cb)
        return unsubscribe

    @property
    def connected(self) -> bool:
        """Return if we're currently connected."""
        return self.state == STATE_CONNECTED

    async def async_send_json_message(
        self, message: Dict[str, Any], message_id: str = None
    ) -> None:
        """Send a message.

        Raises NotConnected if client not connected.
        """
        if self.state != STATE_CONNECTED:
            raise NotConnected

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug("Publishing message:\n%s\n", pprint.pformat(message))

        assert self.client
        # include default messageId if needed
        if message_id is None:
            message["messageId"] = uuid.uuid4().hex
        await self.client.send_json(message)

    async def connect(self) -> None:
        """Connect to the IoT broker."""
        if self.state != STATE_DISCONNECTED:
            raise RuntimeError("Connect called while not disconnected")

        self.close_requested = False
        self.state = STATE_CONNECTING
        self.tries = 0
        self._disconnect_event = asyncio.Event()

        while True:
            try:
                self._logger.debug("Trying to connect")
                await self._handle_connection()
            except Exception:  # pylint: disable=broad-except
                # Safety net. This should never hit.
                # Still adding it here to make sure we can always reconnect
                self._logger.exception("Unexpected error")

            if self.state == STATE_CONNECTED:
                # change state to connecting/disconnected
                self.state = (
                    STATE_DISCONNECTED if self.close_requested else STATE_CONNECTING
                )
                # notify callbacks about disconnection
                if self._on_disconnect:
                    await gather_callbacks(
                        self._logger, "on_disconnect", self._on_disconnect
                    )

            if self.close_requested:
                break

            self.tries += 1

            try:
                await self._wait_retry()
            except asyncio.CancelledError:
                # Happens if disconnect called
                break

        self.state = STATE_DISCONNECTED
        self._disconnect_event.set()
        self._disconnect_event = None

    async def _wait_retry(self) -> None:
        """Wait until it's time till the next retry."""
        # Sleep 2^tries + 0…tries*3 seconds between retries
        self.retry_task = asyncio.create_task(
            asyncio.sleep(2 ** min(9, self.tries) + random.randint(0, self.tries * 3))
        )
        await self.retry_task
        self.retry_task = None

    async def _handle_connection(  # pylint: disable=too-many-branches, too-many-statements
        self,
    ) -> None:
        """Connect to the Z-Wave JS server."""
        client = None
        disconnect_warn = None
        try:
            self.client = client = await self.aiohttp_session.ws_connect(
                self.ws_server_url,
                heartbeat=55,
            )
            self.tries = 0

            version_msg = await client.receive_json()

            self._logger.info(
                "Connected to Home %s (Server %s, Driver %s)",
                version_msg["homeId"],
                version_msg["serverVersion"],
                version_msg["driverVersion"],
            )
            self.state = STATE_CONNECTED

            if self._on_connect:
                await gather_callbacks(self._logger, "on_connect", self._on_connect)

            await self.async_send_json_message({"command": "start_listening"})

            while not client.closed:
                msg = await client.receive()

                if msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING):
                    break

                if msg.type == WSMsgType.ERROR:
                    disconnect_warn = "Connection error"
                    break

                if msg.type != WSMsgType.TEXT:
                    disconnect_warn = "Received non-Text message: {}".format(msg.type)
                    break

                try:
                    msg = msg.json()
                except ValueError:
                    disconnect_warn = "Received invalid JSON."
                    break

                if self._logger.isEnabledFor(logging.DEBUG):
                    self._logger.debug("Received message:\n%s\n", pprint.pformat(msg))

                msg_ = cast(dict, msg)
                try:
                    self.async_handle_message(msg_)

                except InvalidState as err:
                    disconnect_warn = f"Invalid state: {err}"
                    await client.close()
                    break

                except Exception:  # pylint: disable=broad-except
                    self._logger.exception("Unexpected error handling %s", msg)
                    break

        except client_exceptions.WSServerHandshakeError as err:
            self._logger.warning("Unable to connect: %s", err)

        except client_exceptions.ClientError as err:
            self._logger.warning("Unable to connect: %s", err)

        except asyncio.CancelledError:
            pass

        finally:
            if disconnect_warn is None:
                self._logger.info("Connection closed")
            else:
                self._logger.warning("Connection closed: %s", disconnect_warn)

    async def disconnect(self) -> None:
        """Disconnect the client."""
        self.close_requested = True

        if self.client is not None:
            await self.client.close()
        elif self.retry_task is not None:
            self.retry_task.cancel()

        if self._disconnect_event is not None:
            await self._disconnect_event.wait()
