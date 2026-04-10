import asyncio
import json
import logging
from typing import Callable, Optional
import aiohttp

logger = logging.getLogger(__name__)
WS_URL = "wss://api.hyperliquid.xyz/ws"


class HLWebSocket:
    """
    Одне WebSocket з'єднання до Hyperliquid.
    Підписується на userFills + userEvents для кількох адрес.
    При події викликає on_event(address, event_type, data).
    """

    def __init__(self, on_event: Callable):
        self.on_event = on_event
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._subscribed: set[str] = set()
        self._pending: set[str] = set()
        self._running = False
        self._reconnect_delay = 3

    async def start(self):
        self._running = True
        asyncio.create_task(self._run_loop())

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    async def subscribe(self, address: str):
        address = address.lower()
        if address in self._subscribed:
            return
        self._pending.add(address)
        if self._ws and not self._ws.closed:
            await self._do_subscribe(address)

    async def unsubscribe(self, address: str):
        address = address.lower()
        self._subscribed.discard(address)
        self._pending.discard(address)
        if self._ws and not self._ws.closed:
            for sub_type in ("userFills", "userEvents"):
                try:
                    await self._ws.send_json({
                        "method": "unsubscribe",
                        "subscription": {"type": sub_type, "user": address}
                    })
                except Exception:
                    pass

    async def _do_subscribe(self, address: str):
        try:
            for sub_type in ("userFills", "userEvents"):
                await self._ws.send_json({
                    "method": "subscribe",
                    "subscription": {"type": sub_type, "user": address}
                })
            self._subscribed.add(address)
            self._pending.discard(address)
            logger.info(f"WS subscribed: {address[:8]}...")
        except Exception as e:
            logger.error(f"WS subscribe error {address[:8]}: {e}")

    async def _run_loop(self):
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.error(f"WS connection failed: {e}")
            if self._running:
                logger.info(f"WS reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _connect(self):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                WS_URL,
                heartbeat=20,
            ) as ws:
                self._ws = ws
                self._reconnect_delay = 3
                logger.info("WS connected to Hyperliquid")

                all_addrs = self._subscribed | self._pending
                self._subscribed.clear()
                for addr in all_addrs:
                    await self._do_subscribe(addr)

                # Ping щоб тримати з'єднання живим
                asyncio.create_task(self._ping_loop(ws))

                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        logger.warning(f"WS closed/error: {msg.type}")
                        break

    async def _ping_loop(self, ws):
        """Відправляє ping кожні 30с щоб з'єднання не рвалось."""
        try:
            while not ws.closed:
                await asyncio.sleep(30)
                if not ws.closed:
                    await ws.send_json({"method": "ping"})
        except Exception:
            pass

    async def _handle(self, raw: str):
        try:
            msg = json.loads(raw)
            channel = msg.get("channel")
            data = msg.get("data", {})

            if channel == "userFills":
                user = data.get("user", "").lower()
                fills = data.get("fills", [])
                if user and fills:
                    await self.on_event(user, "fills", fills)

            elif channel == "userEvents":
                user = data.get("user", "").lower()
                if user:
                    await self.on_event(user, "events", data)

        except Exception as e:
            logger.error(f"WS handle error: {e}")
