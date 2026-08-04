import asyncio
import logging
import threading

from . import protocol
from .helpers import format_hex, log_trace, log_info, log_warning, log_exception

logger = logging.getLogger(__name__)
logger.propagate = True


class Framer:
    """Async BLE transport plumbing for the Madoka protocol.

    Owns the per-device notification queue, thread-safety for BLE callbacks
    (which may arrive on a different thread than the event loop), and
    request/response correlation by cmd. Frame encoding/decoding itself is
    protocol.py's job - this class is just the async glue around it, so it
    can't be exercised without a running event loop the way protocol.py can.
    """

    def __init__(self, loop, owner=None):
        self._loop = loop
        self._owner = owner  # only used for log-context labeling
        self._lock = threading.Lock()
        self._queue = asyncio.Queue(maxsize=1)
        self._parser = protocol.FrameParser()

    def feed_chunk(self, data: bytearray):
        """Feed one raw notification chunk in, from any thread. Once a full
        packet has been reassembled and decoded, enqueues (cmd, msg) for
        wait_for()."""
        log_trace(logger, f"feed_chunk: ({len(data)}){format_hex(data)}", self._owner)

        with self._lock:
            result = self._parser.feed(data)

        if result is None:
            return

        cmd, msg = result
        log_trace(logger, f"feed_chunk decoded cmd:{cmd:x} {format_hex(msg)}", self._owner)

        # enqueue in a thread-safe manner, tagged with the cmd this is a
        # response to, so wait_for() can verify it's not stale
        self._enqueue(cmd, msg)

    def _enqueue(self, cmd, msg):
        try:
            self._loop.call_soon_threadsafe(self._put_nowait, (cmd, msg))
        except Exception:
            try:
                asyncio.run_coroutine_threadsafe(self._queue.put((cmd, msg)), self._loop)
            except Exception:
                log_exception(logger, "failed to enqueue notification", self._owner)

    def _put_nowait(self, item):
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            log_warning(logger, "notification queue already has a pending packet", self._owner)

    async def wait_for(self, expected_cmd, timeout=0.5):
        """Wait for the response to expected_cmd, discarding anything else
        that arrives (e.g. a late response to an earlier, already-timed-out
        request) within the same timeout budget instead of returning it."""
        log_trace(logger, f"wait_for expecting cmd:{expected_cmd:x}", self._owner)
        deadline = self._loop.time() + timeout
        while True:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                log_info(logger, "wait_for timeout", self._owner)
                return None

            try:
                rx_cmd, msg = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                log_trace(logger, f"wait_for got cmd:{rx_cmd:x} {format_hex(msg)}", self._owner)
            except asyncio.TimeoutError:
                log_info(logger, "wait_for timeout", self._owner)
                return None

            if rx_cmd != expected_cmd:
                log_warning(logger, f"wait_for: discarding stale response cmd:{rx_cmd:x}, expected:{expected_cmd:x}", self._owner)
                continue

            return msg

    def drain(self):
        """Discard any pending queued response. Returns the count discarded."""
        deleted = 0
        while True:
            try:
                self._queue.get_nowait()
                deleted += 1
            except asyncio.QueueEmpty:
                break
        return deleted

    def reset(self):
        """Full reset: clear in-progress reassembly state and any pending
        queued response. Call on disconnect."""
        with self._lock:
            self._parser.reset()
        self.drain()
