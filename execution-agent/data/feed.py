"""
data/feed.py — Live ES futures feed using the Webull Python SDK.

Replaces the previous custom aiomqtt implementation. The SDK's DataStreamingClient
wraps paho-mqtt and handles all authentication (token exchange, signing, reconnect)
automatically — no manual MQTT password building or broker-endpoint calls needed.

Two modes:
  1. Streaming (preferred): DataStreamingClient subscribes to the ES futures
     snapshot topic via MQTT. Each incoming snapshot is forwarded to BarNormalizer.
  2. Polling fallback: If the MQTT subscription fails, DataClient polls
     get_futures_snapshot() every POLL_INTERVAL_SECONDS. Less efficient but
     sufficient for a classifier that fires once per day.

The streaming client runs in a background thread (paho-mqtt is synchronous/threaded,
not async). Thread-safe: BarNormalizer is append-only with a deque(maxlen).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import pytz

import config
from data.normalizer import Bar, BarNormalizer

LOGGER = logging.getLogger("infiniteloop.data.feed")

EASTERN = pytz.timezone("US/Eastern")

# ES continuous front-month contract symbol for Webull futures feed
ES_SYMBOL   = "ESc1"
ES_CATEGORY = "US_FUTURES"

# SDK streaming sub-type for real-time snapshot updates
STREAM_SUB_TYPE = "snapshot"

# Fallback polling interval (seconds) when streaming is unavailable
POLL_INTERVAL_SECONDS = 30

# Feed considered stalled if no update in this many seconds
STALL_THRESHOLD_SECONDS = 120


class WebullFeed:
    """
    Live ES futures price feed using the Webull SDK.

    Usage (from an async context):
        feed = WebullFeed(api_client=config.build_api_client(), on_bar=my_callback)
        await feed.start()
        ...
        bars = feed.rth_bars_today()
        await feed.stop()
    """

    def __init__(
        self,
        api_client=None,
        on_bar: Optional[Callable[[Bar], None]] = None,
    ) -> None:
        self._api_client       = api_client or config.build_api_client()
        self._normalizer       = BarNormalizer(on_bar=on_bar)
        self._streaming_client = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event       = threading.Event()
        self.last_tick_at: Optional[float] = None
        self._mode = "stopped"  # streaming | polling | stopped

    # ── Public interface ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the feed. Tries streaming first, falls back to polling."""
        self._stop_event.clear()
        if self._try_start_streaming():
            self._mode = "streaming"
            LOGGER.info("WebullFeed started in STREAMING mode (MQTT via SDK)")
        else:
            self._start_polling()
            self._mode = "polling"
            LOGGER.warning(
                "WebullFeed streaming unavailable — using POLLING mode "
                "(snapshot every %ds)", POLL_INTERVAL_SECONDS,
            )

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._stop_event.set()
        if self._streaming_client:
            try:
                self._streaming_client.unsubscribe(unsubscribe_all=True)
                self._streaming_client.disconnect()
            except Exception as exc:
                LOGGER.warning("Streaming client disconnect error: %s", exc)
            self._streaming_client = None
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        self._mode = "stopped"
        LOGGER.info("WebullFeed stopped")

    @property
    def last_price(self) -> Optional[float]:
        return self._normalizer.last_price

    def rth_bars_today(self) -> list[Bar]:
        return self._normalizer.rth_bars_today()

    def latest_bars(self, n: int = 60) -> list[Bar]:
        return self._normalizer.latest_bars(n)

    def is_stalled(self) -> bool:
        """True if no tick has arrived in STALL_THRESHOLD_SECONDS."""
        if self.last_tick_at is None:
            return False   # never received anything yet — don't alarm before market open
        return (time.time() - self.last_tick_at) > STALL_THRESHOLD_SECONDS

    # ── Streaming (MQTT via SDK DataStreamingClient) ───────────────────────────

    def _try_start_streaming(self) -> bool:
        """Attempt to start the SDK DataStreamingClient. Returns True on success."""
        try:
            from webull.data.data_streaming_client import DataStreamingClient

            session_id = str(uuid.uuid4())
            client = DataStreamingClient(
                app_key=config.WEBULL_APP_KEY,
                app_secret=config.WEBULL_APP_SECRET,
                region_id=config.WEBULL_REGION_ID,
                session_id=session_id,
                http_host=config.WEBULL_STREAM_HOST,
                tls_enable=True,
            )

            def on_connect(streaming_client, userdata, session_id):
                LOGGER.info("MQTT connected — subscribing to %s", ES_SYMBOL)
                streaming_client.subscribe(
                    symbols=ES_SYMBOL,
                    category=ES_CATEGORY,
                    sub_types=[STREAM_SUB_TYPE],
                )

            def on_subscribe_success(streaming_client, result, session_id):
                LOGGER.info("Subscribed to %s snapshot feed", ES_SYMBOL)

            def on_message(streaming_client, topic, result):
                try:
                    self._handle_snapshot(result)
                except Exception as exc:
                    LOGGER.warning("Snapshot handling error: %s", exc)

            client.on_connect_success  = on_connect
            client.on_subscribe_success = on_subscribe_success
            client.on_message = on_message

            # connect() is non-blocking — paho starts its own background thread
            client.connect()
            self._streaming_client = client
            return True

        except Exception as exc:
            LOGGER.warning("Could not start streaming client: %s", exc)
            return False

    def _handle_snapshot(self, result) -> None:
        """
        Convert an SDK SnapshotResult (or raw dict from HTTP poll) to a tick
        and forward to BarNormalizer.
        """
        # SnapshotResult from MQTT streaming
        if hasattr(result, "price") and result.price is not None:
            price  = float(result.price)
            volume = int(getattr(result, "volume", None) or 1)
        # Plain dict from HTTP polling fallback
        elif isinstance(result, dict):
            price  = float(result.get("close") or result.get("price") or 0)
            volume = int(result.get("volume") or 1)
        else:
            return

        if price <= 0:
            return

        ts = datetime.now(timezone.utc)
        self.last_tick_at = time.time()
        self._normalizer.on_tick(price=price, size=volume, ts=ts)

    # ── Polling fallback ───────────────────────────────────────────────────────

    def _start_polling(self) -> None:
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="webull-poll", daemon=True
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        """Background thread: poll get_futures_snapshot() every N seconds."""
        from webull.data.data_client import DataClient
        from webull.data.common.category import Category

        data_client = DataClient(self._api_client)

        while not self._stop_event.is_set():
            try:
                resp = data_client.futures_market_data.get_futures_snapshot(
                    symbols=[ES_SYMBOL],
                    category=Category.US_FUTURES,
                )
                # SDK returns the API response object; parse the body
                if hasattr(resp, "body") and resp.body:
                    items = resp.body if isinstance(resp.body, list) else [resp.body]
                    for item in items:
                        self._handle_snapshot(
                            item.__dict__ if hasattr(item, "__dict__") else item
                        )
                else:
                    LOGGER.warning("Futures snapshot poll returned empty body")
            except Exception as exc:
                LOGGER.error("Futures snapshot poll error: %s", exc)

            self._stop_event.wait(POLL_INTERVAL_SECONDS)
