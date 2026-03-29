"""Halo audio -- mic input and speaker output via sounddevice (PortAudio)"""

import queue
import threading
import time
from typing import Optional

import numpy as np
import sounddevice as sd

from modules.logger import get_logger

# Gemini Live expects 16kHz mono 16-bit PCM
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 1600  # 100ms chunks at 16kHz

# Speaker output -- Gemini sends 24kHz PCM
OUTPUT_SAMPLE_RATE = 24000


class AudioInput:
    """Streams mic audio as PCM chunks."""

    def __init__(self):
        self.logger = get_logger()
        self._queue: queue.Queue = queue.Queue(maxsize=100)
        self._stream: Optional[sd.InputStream] = None
        self._running = False

    def _callback(self, indata, frames, time_info, status):
        if status:
            self.logger.debug(f"AUDIO:input status: {status}")
        if self._running:
            try:
                self._queue.put_nowait(indata.copy().tobytes())
            except queue.Full:
                pass  # Drop oldest if consumer is slow

    def start(self, device=None):
        if self._running:
            return
        self._running = True
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCK_SIZE,
            device=device,
            callback=self._callback,
        )
        self._stream.start()
        self.logger.info("AUDIO:mic started")

    def stop(self):
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.logger.info("AUDIO:mic stopped")

    def read_chunk(self, timeout: float = 0.5) -> Optional[bytes]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self):
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


class AudioOutput:
    """Plays PCM audio chunks to speaker using a continuous byte buffer."""

    def __init__(self):
        self.logger = get_logger()
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream: Optional[sd.OutputStream] = None
        self._running = False

    def _callback(self, outdata, frames, time_info, status):
        if status:
            self.logger.debug(f"AUDIO:output status: {status}")
        need_bytes = frames * 2  # 16-bit = 2 bytes per sample
        with self._lock:
            if len(self._buffer) >= need_bytes:
                chunk = bytes(self._buffer[:need_bytes])
                del self._buffer[:need_bytes]
            elif len(self._buffer) > 0:
                # Pad remainder with silence
                chunk = bytes(self._buffer) + b"\x00" * (need_bytes - len(self._buffer))
                self._buffer.clear()
            else:
                outdata[:] = np.zeros((frames, 1), dtype=DTYPE)
                return
        arr = np.frombuffer(chunk, dtype=DTYPE)
        outdata[:] = arr.reshape(-1, 1)

    def start(self, device=None, sample_rate: int = OUTPUT_SAMPLE_RATE):
        if self._running:
            return
        self._running = True
        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCK_SIZE,
            device=device,
            callback=self._callback,
        )
        self._stream.start()
        self.logger.info(f"AUDIO:speaker started ({sample_rate}Hz)")

    def stop(self):
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.logger.info("AUDIO:speaker stopped")

    def play_chunk(self, pcm_data: bytes):
        with self._lock:
            self._buffer.extend(pcm_data)

    def drain(self):
        with self._lock:
            self._buffer.clear()

    def is_playing(self) -> bool:
        with self._lock:
            return len(self._buffer) > 0


def list_devices() -> list[dict]:
    """List available audio devices."""
    devices = sd.query_devices()
    result = []
    for i, d in enumerate(devices):
        result.append({
            "index": i,
            "name": d["name"],
            "inputs": d["max_input_channels"],
            "outputs": d["max_output_channels"],
            "default_sr": d["default_samplerate"],
        })
    return result


def get_default_devices() -> dict:
    """Get default input and output device info."""
    try:
        in_dev = sd.query_devices(kind="input")
        out_dev = sd.query_devices(kind="output")
        return {
            "input": {"name": in_dev["name"], "index": in_dev["index"]},
            "output": {"name": out_dev["name"], "index": out_dev["index"]},
        }
    except Exception as e:
        return {"error": str(e)}


def test_loopback(duration: float = 3.0) -> float:
    """Record for duration seconds, play back, return round-trip latency estimate."""
    logger = get_logger()
    logger.info(f"AUDIO:loopback test -- recording {duration}s")

    recording = sd.rec(
        int(SAMPLE_RATE * duration),
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
    )
    sd.wait()

    start = time.time()
    sd.play(recording, samplerate=SAMPLE_RATE)
    sd.wait()
    latency = (time.time() - start) * 1000

    logger.info(f"AUDIO:loopback latency ~{latency:.0f}ms")
    return latency
