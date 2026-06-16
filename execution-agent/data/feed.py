"""
data/feed.py — Webull MQTT subscriber for live ES futures tick data.

Connects to the Webull OpenAPI MQTT broker, subscribes to the ES continuous
contract tick feed, and forwards each trade tick to a BarNormalizer instance.

Webull MQTT details (from Webull OpenAPI docs):
  - Broker: connect via Webull's API to get the MQTT endpoint + credentials
  - Topic:  market data topics for futures quotes
  - Auth:   token-based, refreshed from the Webull HTTP API

The feed runs in an asyncio task. On disconnect it backs off and reconnects
automatically. The watchdog monitors feed.last_tick_at to detect stalls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import config
from data.normalizer import Bar, BarNormalizer

LOGGER = logging.getLogger("infiniteloop.data.feed")

# Reconnect back-off: start at 2 s, cap at 60 s
_RECONNECT_INITIAL = 2.0
_RECONNECT_MAX     = 60.0

# ES continuous contract symbol used in Webull's market data API
ES_SYMBOL = "ESc1"   # continuous front-month; confirm exact symbol with Webull docs


class WebullFeed:
    """
    Async MQTT feed that streams ES futures ticks from Webull.

    Usage:
        feed = WebullFeed(on_bar=my_callback)
        await feed.start()          # connect and subscribe
        ...
        await feed.stop()           # clean shutdown
    """

    def __init__(self, on_bar=None) -> None:
        self._normalizer = BarNormalizer(on_bar=on_bar)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.last_tick_at: Optional[float] = None   # Unix timestamp of last tick received
        self._reconnect_delay = _RECONNECT_INITIAL

    # ── Public interface ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin streaming. Returns immediately; feed runs in the background."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="webull-feed")
        LOGGER.info("WebullFeed started (symbol=%s)", ES_SYMBOL)

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        LOGGER.info("WebullFeed stopped")

    @property
    def last_price(self) -> Optional[float]:
        return self._normalizer.last_price

    def rth_bars_today(self) -> list[Bar]:
        return self._normalizer.rth_bars_today()

    def latest_bars(self, n: int = 60) -> list[Bar]:
        return self._normalizer.latest_bars(n)

    # ── Internal MQTT loop ─────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Connect → subscribe → receive loop with automatic reconnect."""
        while self._running:
            try:
                await self._connect_and_stream()
                self._reconnect_delay = _RECONNECT_INITIAL   # reset on clean exit
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOGGER.error(
                    "Feed connection error: %s — reconnecting in %.0fs",
                    exc, self._reconnect_delay,
                )
            if not self._running:
                break
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, _RECONNECT_MAX)

    async def _connect_and_stream(self) -> None:
        """
        Establish the Webull MQTT connection and stream ticks until disconnect.

        Webull OpenAPI MQTT flow:
          1. GET /quote/broker/ip  → {host, port, path}  (broker endpoint)
          2. Subscribe with app_key + token for auth
          3. Publish subscription message for ES ticks
          4. Receive Quote messages → parse → forward to normalizer

        We use the `asyncio-mqtt` (aiomqtt) library which wraps paho-mqtt
        with async support.
        """
        import aiomqtt

        broker_info = await self._get_broker_info()
        host = broker_info["host"]
        port = int(broker_info.get("port", 1883))

        username = config.WEBULL_APP_KEY
        password = self._build_mqtt_password()

        LOGGER.info("Connecting to Webull MQTT broker %s:%d", host, port)

        async with aiomqtt.Client(
            hostname=host,
            port=port,
            username=username,
            password=password,
            keepalive=30,
        ) as client:
            # Subscribe to the ES futures quote topic
            topic = f"quotes/futures/{ES_SYMBOL}"
            await client.subscribe(topic)
            LOGGER.info("Subscribed to MQTT topic: %s", topic)
            self._reconnect_delay = _RECONNECT_INITIAL  # successful connect

            async for message in client.messages:
                if not self._running:
                    break
                try:
                    self._handle_message(message.payload)
                except Exception as exc:
                    LOGGER.warning("Tick parse error: %s", exc)

    async def _get_broker_info(self) -> dict:
        """
        Call the Webull HTTP API to get the current MQTT broker endpoint.
        Webull rotates broker IPs so this must be called fresh each connect.
        """
        import aiohttp

        url = "https://quoteapi.webull.com/api/quote/broker/ip"
        headers = {
            "App": "desktop",
            "App-Group": "broker",
            "Appid": config.WEBULL_APP_KEY,
            "Did": config.WEBULL_ACCOUNT_ID,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                data = await resp.json()
                LOGGER.debug("Broker info: %s", data)
                return data

    def _build_mqtt_password(self) -> str:
        """
        Build the MQTT password token.
        Webull uses: base64(app_key + ':' + trade_token) or similar.
        Check the Webull OpenAPI auth docs for the exact format.
        """
        import base64
        raw = f"{config.WEBULL_APP_KEY}:{config.WEBULL_TRADE_TOKEN}"
        return base64.b64encode(raw.encode()).decode()

    def _handle_message(self, payload: bytes) -> None:
        """Parse a raw MQTT message and forward the tick to the normalizer."""
        data = json.loads(payload)

        # Webull quote message format (adapt to actual proto/JSON schema):
        # { "type": "quote", "tickerId": ..., "tradeTime": <ms epoch>,
        #   "close": "5750.25", "volume": "10" }
        msg_type = data.get("type", "")
        if msg_type not in ("quote", "trade", "tick"):
            return

        price_raw = data.get("close") or data.get("price") or data.get("lastPrice")
        if price_raw is None:
            return

        price  = float(price_raw)
        size   = int(data.get("volume", 0) or data.get("size", 0) or 1)
        ts_ms  = data.get("tradeTime") or data.get("timestamp") or (time.time() * 1000)
        ts     = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc)

        self.last_tick_at = time.time()
        self._normalizer.on_tick(price=price, size=size, ts=ts)
