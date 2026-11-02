"""Ultra-low-latency PC Audio Output Engine using `sounddevice.RawOutputStream` and `numpy`."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
import sounddevice as sd

logger = logging.getLogger("imic.audio")

SAMPLE_RATE = 48_000
BLOCKSIZE = 256  # 256 frames @ 48kHz = 5.33 ms (< 10ms target)
BYTES_PER_SAMPLE = 2  # int16
TARGET_DEVICE_SUBSTRING = "cable input"  # VB-Audio Virtual Cable
SECONDARY_VIRTUAL_DEVICES = ("vb-audio", "blackhole", "virtual cable", "loopback", "background music")


def _hostapi_priority(hostapi_name: str) -> int:
    """Rank host APIs for lowest output latency (WASAPI first on Windows)."""
    lower = hostapi_name.lower()
    if "wasapi" in lower:
        return 0
    if "core audio" in lower or "pipewire" in lower or "alsa" in lower:
        return 1
    if "wdm-ks" in lower:
        return 2
    if "directsound" in lower:
        return 3
    if "mme" in lower:
        return 4
    return 5


@dataclass
class OutputDeviceInfo:
    index: int
    name: str
    hostapi: str
    max_output_channels: int
    default_low_output_latency_ms: float
    is_cable_input: bool
    is_virtual: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "hostapi": self.hostapi,
            "max_output_channels": self.max_output_channels,
            "default_low_output_latency_ms": round(self.default_low_output_latency_ms, 2),
            "is_cable_input": self.is_cable_input,
            "is_virtual": self.is_virtual,
        }


class LowLatencyRingBuffer:
    """Thread-safe bounded byte ring buffer that auto-trims drift to keep latency < 10ms."""

    def __init__(self, max_frames: int = 960, trim_to_frames: int = 384) -> None:
        # 384 frames @ 48kHz = 8.0 ms; 960 frames = 20.0 ms drift ceiling
        self._max_bytes = max_frames * BYTES_PER_SAMPLE
        self._trim_bytes = trim_to_frames * BYTES_PER_SAMPLE
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.underflows = 0
        self.drift_trims = 0
        self.frames_written = 0

    def write(self, data: bytes | bytearray | memoryview) -> None:
        nbytes = len(data)
        if nbytes == 0:
            return
        # Ensure sample alignment (2 bytes per int16 frame)
        if nbytes & 1:
            data = memoryview(data)[: nbytes - 1]
            nbytes -= 1
            if nbytes == 0:
                return

        with self._lock:
            self._buf.extend(data)
            self.frames_written += nbytes // BYTES_PER_SAMPLE
            if len(self._buf) > self._max_bytes:
                drop = len(self._buf) - self._trim_bytes
                # Keep 2-byte int16 sample alignment
                drop &= ~1
                del self._buf[:drop]
                self.drift_trims += 1

    def read_into(self, out_bytes: int) -> bytes:
        with self._lock:
            avail = len(self._buf)
            if avail >= out_bytes:
                chunk = bytes(self._buf[:out_bytes])
                del self._buf[:out_bytes]
                return chunk
            if avail > 0:
                chunk = bytes(self._buf) + bytes(out_bytes - avail)
                self._buf.clear()
                self.underflows += 1
                return chunk
            self.underflows += 1
            return b""

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    @property
    def buffered_frames(self) -> int:
        with self._lock:
            return len(self._buf) // BYTES_PER_SAMPLE

    @property
    def buffered_ms(self) -> float:
        return (self.buffered_frames / SAMPLE_RATE) * 1000.0


class AudioOutputEngine:
    """Manages `sounddevice.RawOutputStream` routed to 'CABLE Input' (or fallback)."""

    def __init__(
        self,
        samplerate: int = SAMPLE_RATE,
        blocksize: int = BLOCKSIZE,
        preferred_device: str | int | None = None,
    ) -> None:
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.preferred_device = preferred_device or os.environ.get("IMIC_OUTPUT_DEVICE")
        self.ring = LowLatencyRingBuffer(max_frames=960, trim_to_frames=384)

        self._stream: sd.RawOutputStream | None = None
        self._stream_lock = threading.Lock()
        self._active_channels = 1
        self._zero_mono = bytes(blocksize * BYTES_PER_SAMPLE)
        self._zero_stereo = bytes(blocksize * BYTES_PER_SAMPLE * 2)

        self.device_info: OutputDeviceInfo | None = None
        self.cable_input_found = False
        self.null_sink_mode = False
        self.last_rms_dbfs: float = -100.0
        self.last_peak: float = 0.0

    @staticmethod
    def list_output_devices() -> list[OutputDeviceInfo]:
        """Return all available output audio devices with host API metadata."""
        try:
            raw_devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception as exc:
            logger.warning("Unable to query audio devices: %s", exc)
            return []

        devices: list[OutputDeviceInfo] = []
        for idx, dev in enumerate(raw_devices):
            max_out = int(dev.get("max_output_channels", 0))
            if max_out <= 0:
                continue
            hostapi_idx = int(dev.get("hostapi", 0))
            hostapi_name = (
                hostapis[hostapi_idx].get("name", "Unknown")
                if 0 <= hostapi_idx < len(hostapis)
                else "Unknown"
            )
            name = str(dev.get("name", f"Device {idx}"))
            lower_name = name.lower()
            is_cable = TARGET_DEVICE_SUBSTRING in lower_name
            is_virtual = is_cable or any(v in lower_name for v in SECONDARY_VIRTUAL_DEVICES)
            low_lat_ms = float(dev.get("default_low_output_latency", 0.0)) * 1000.0

            devices.append(
                OutputDeviceInfo(
                    index=idx,
                    name=name,
                    hostapi=hostapi_name,
                    max_output_channels=max_out,
                    default_low_output_latency_ms=low_lat_ms,
                    is_cable_input=is_cable,
                    is_virtual=is_virtual,
                )
            )
        return devices

    def select_target_device(self, override: str | int | None = None) -> OutputDeviceInfo | None:
        """Select 'CABLE Input' (VB-Audio Virtual Cable), secondary virtual cable, or default."""
        devices = self.list_output_devices()
        if not devices:
            return None

        target = override if override is not None else self.preferred_device
        if target is not None:
            if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
                target_idx = int(target)
                for dev in devices:
                    if dev.index == target_idx:
                        return dev
            elif isinstance(target, str):
                needle = target.lower()
                matches = [d for d in devices if needle in d.name.lower()]
                if matches:
                    matches.sort(key=lambda d: _hostapi_priority(d.hostapi))
                    return matches[0]

        # 1. Primary requirement: VB-Audio Virtual Cable ("CABLE Input")
        cable_matches = [d for d in devices if d.is_cable_input]
        if cable_matches:
            cable_matches.sort(key=lambda d: _hostapi_priority(d.hostapi))
            return cable_matches[0]

        # 2. Secondary virtual audio cables (e.g., BlackHole / VB-Audio)
        virtual_matches = [d for d in devices if d.is_virtual]
        if virtual_matches:
            virtual_matches.sort(key=lambda d: _hostapi_priority(d.hostapi))
            return virtual_matches[0]

        # 3. System default output device fallback
        try:
            default_out = sd.default.device[1]
            if default_out is not None and int(default_out) >= 0:
                for dev in devices:
                    if dev.index == int(default_out):
                        return dev
        except Exception:
            pass

        return devices[0]

    def _audio_callback(
        self,
        outdata: Any,
        frames: int,
        time_info: Any,  # noqa: ARG002
        status: sd.CallbackFlags,
    ) -> None:
        """Real-time PortAudio callback pulling 16-bit PCM frames from the ring buffer."""
        if status.output_underflow:
            self.ring.underflows += 1

        mono_bytes_needed = frames * BYTES_PER_SAMPLE
        chunk = self.ring.read_into(mono_bytes_needed)

        if self._active_channels == 1:
            if chunk:
                outdata[:] = chunk
            elif frames == self.blocksize:
                outdata[:] = self._zero_mono
            else:
                outdata[:] = bytes(mono_bytes_needed)
        else:
            # Driver required stereo output: duplicate mono int16 across L & R via numpy
            if chunk:
                mono_np = np.frombuffer(chunk, dtype=np.int16)
                stereo_np = np.repeat(mono_np, 2)
                outdata[:] = stereo_np.tobytes()
            elif frames == self.blocksize:
                outdata[:] = self._zero_stereo
            else:
                outdata[:] = bytes(mono_bytes_needed * self._active_channels)

    def start(self, device_override: str | int | None = None) -> None:
        """Open and start `sounddevice.RawOutputStream` with blocksize=256 and latency='low'."""
        with self._stream_lock:
            self._close_locked()
            self.ring.clear()

            selected = self.select_target_device(device_override)
            if selected is None:
                self.null_sink_mode = True
                self.device_info = None
                self.cable_input_found = False
                logger.warning("No audio output devices found; running in null-sink mode.")
                return

            self.device_info = selected
            self.cable_input_found = selected.is_cable_input
            if not self.cable_input_found:
                logger.warning(
                    "'CABLE Input' (VB-Audio Virtual Cable) not found. "
                    "Routing audio to fallback device [%d] '%s' (%s).",
                    selected.index,
                    selected.name,
                    selected.hostapi,
                )

            # Open mono RawOutputStream as specified: blocksize=256, latency='low', int16, 48kHz
            for channels in (1, 2):
                if channels > selected.max_output_channels:
                    continue
                try:
                    self._active_channels = channels
                    stream = sd.RawOutputStream(
                        samplerate=self.samplerate,
                        blocksize=self.blocksize,
                        device=selected.index,
                        channels=channels,
                        dtype="int16",
                        latency="low",
                        callback=self._audio_callback,
                    )
                    stream.start()
                    self._stream = stream
                    self.null_sink_mode = False
                    logger.info(
                        "Started RawOutputStream on [%d] '%s' (%s) | %d Hz | blocksize=%d (%.2f ms) | channels=%d",
                        selected.index,
                        selected.name,
                        selected.hostapi,
                        self.samplerate,
                        self.blocksize,
                        (self.blocksize / self.samplerate) * 1000.0,
                        channels,
                    )
                    return
                except Exception as exc:
                    logger.warning(
                        "Failed opening RawOutputStream (channels=%d) on '%s': %s",
                        channels,
                        selected.name,
                        exc,
                    )

            self.null_sink_mode = True
            logger.error("Could not open RawOutputStream on '%s'; running in null-sink mode.", selected.name)

    def push_pcm16(self, pcm_bytes: bytes) -> None:
        """Ingest raw 16-bit little-endian mono PCM packet from WebSocket."""
        if not pcm_bytes:
            return

        # Compute instantaneous level stats with numpy (zero-copy view)
        sample_count = len(pcm_bytes) // BYTES_PER_SAMPLE
        if sample_count > 0:
            samples = np.frombuffer(pcm_bytes, dtype="<i2", count=sample_count)
            norm = samples.astype(np.float32) * (1.0 / 32768.0)
            peak = float(np.max(np.abs(norm)))
            rms = float(np.sqrt(np.mean(norm * norm)))
            self.last_peak = peak
            self.last_rms_dbfs = 20.0 * float(np.log10(max(rms, 1e-5)))

        if not self.null_sink_mode:
            self.ring.write(pcm_bytes)

    def reset_buffer(self) -> None:
        """Flush buffered audio when a client disconnects or mutes."""
        self.ring.clear()
        self.last_peak = 0.0
        self.last_rms_dbfs = -100.0

    def switch_device(self, device_index: int) -> dict[str, Any]:
        """Dynamically switch output audio device."""
        self.start(device_override=device_index)
        return self.status_dict()

    def _close_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def stop(self) -> None:
        with self._stream_lock:
            self._close_locked()
            self.ring.clear()

    def summary_line(self) -> str:
        if self.null_sink_mode or self.device_info is None:
            return "Null Sink (no hardware/virtual output device available)"
        tag = "VB-CABLE ACTIVE" if self.cable_input_found else "FALLBACK OUTPUT"
        block_ms = (self.blocksize / self.samplerate) * 1000.0
        return (
            f"[{tag}] {self.device_info.name} ({self.device_info.hostapi}) | "
            f"{self.samplerate}Hz PCM16 | blocksize={self.blocksize} ({block_ms:.2f}ms)"
        )

    def status_dict(self) -> dict[str, Any]:
        block_ms = (self.blocksize / self.samplerate) * 1000.0
        stream_lat_ms = (
            float(self._stream.latency) * 1000.0 if self._stream is not None else 0.0
        )
        return {
            "active": self._stream is not None and bool(self._stream.active),
            "null_sink_mode": self.null_sink_mode,
            "cable_input_found": self.cable_input_found,
            "samplerate": self.samplerate,
            "blocksize": self.blocksize,
            "block_ms": round(block_ms, 2),
            "ring_buffer_ms": round(self.ring.buffered_ms, 2),
            "driver_latency_ms": round(stream_lat_ms, 2),
            "channels": self._active_channels,
            "device": self.device_info.to_dict() if self.device_info else None,
            "rms_dbfs": round(self.last_rms_dbfs, 1),
            "peak": round(self.last_peak, 3),
            "underflows": self.ring.underflows,
            "drift_trims": self.ring.drift_trims,
        }
