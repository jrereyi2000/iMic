"""End-to-end verification tests for iMic audio pipeline, ADB reverse, and HTTP/WS server."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.request

import numpy as np
import websockets

from app.audio_engine import AudioOutputEngine, LowLatencyRingBuffer
from app.usb_adb import discover_network_endpoints, find_adb_binary, setup_adb_reverse


class UnitPipelineTests(unittest.TestCase):
    def test_ring_buffer_drift_cap_keeps_latency_under_10ms(self) -> None:
        ring = LowLatencyRingBuffer(max_frames=960, trim_to_frames=384)
        # Push 2000 frames (~41.6ms burst) of 16-bit PCM
        burst = np.zeros(2000, dtype="<i2").tobytes()
        ring.write(burst)
        self.assertEqual(ring.buffered_frames, 384)
        self.assertLess(ring.buffered_ms, 10.0)
        self.assertEqual(ring.drift_trims, 1)

    def test_cable_input_wasapi_selection_priority(self) -> None:
        fake_devices = [
            {
                "name": "Speakers (Realtek Audio)",
                "hostapi": 0,
                "max_output_channels": 2,
                "default_low_output_latency": 0.02,
            },
            {
                "name": "CABLE Input (VB-Audio Virtual Cable)",
                "hostapi": 0,
                "max_output_channels": 2,
                "default_low_output_latency": 0.09,
            },
            {
                "name": "CABLE Input (VB-Audio Virtual Cable)",
                "hostapi": 1,
                "max_output_channels": 2,
                "default_low_output_latency": 0.003,
            },
        ]
        fake_hostapis = [{"name": "MME"}, {"name": "Windows WASAPI"}]

        with (
            patch("sounddevice.query_devices", return_value=fake_devices),
            patch("sounddevice.query_hostapis", return_value=fake_hostapis),
        ):
            engine = AudioOutputEngine()
            chosen = engine.select_target_device()
            self.assertIsNotNone(chosen)
            assert chosen is not None
            self.assertTrue(chosen.is_cable_input)
            self.assertEqual(chosen.hostapi, "Windows WASAPI")
            self.assertEqual(chosen.index, 2)

    def test_local_platform_tools_adb_reverse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pt_dir = Path(tmp) / "platform-tools"
            pt_dir.mkdir(parents=True)
            adb_script = pt_dir / "adb"
            log_file = Path(tmp) / "adb_args.txt"
            adb_script.write_text(
                f"#!/bin/sh\necho \"$@\" >> '{log_file}'\n"
                "if [ \"$1\" = \"devices\" ]; then\n"
                "  printf 'List of devices attached\\nPIXEL9USB\\tdevice\\n'\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            adb_script.chmod(adb_script.stat().st_mode | stat.S_IXUSR)

            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with patch("shutil.which", return_value=None):
                    found = find_adb_binary()
                    self.assertEqual(found, str(adb_script.resolve()))
                    res = setup_adb_reverse(port=8000)
                    self.assertTrue(res.adb_found)
                    self.assertTrue(res.tunnel_active)
                    self.assertIn("PIXEL9USB", res.devices)
                    logged = log_file.read_text(encoding="utf-8")
                    self.assertIn("reverse tcp:8000 tcp:8000", logged)
            finally:
                os.chdir(old_cwd)

    def test_network_discovery_runs_cleanly(self) -> None:
        endpoints = discover_network_endpoints(port=8000)
        self.assertIsInstance(endpoints, list)


async def verify_live_server(base_http: str = "http://127.0.0.1:8000", base_ws: str = "ws://127.0.0.1:8000") -> None:
    # 1. Verify `/` HTML UI
    with urllib.request.urlopen(f"{base_http}/", timeout=5) as resp:
        assert resp.status == 200
        ctype = resp.headers.get("Content-Type", "")
        assert "text/html" in ctype, f"Unexpected HTML Content-Type: {ctype}"
        html = resp.read().decode("utf-8")
        assert "visualizerCanvas" in html
        assert "gainSlider" in html
        assert "muteBtn" in html

    # 2. Verify AudioWorklet JS processor MIME type (no MIME-type errors!)
    for path in ("/pcm-processor.js", "/static/pcm-processor.js", "/static/app.js"):
        with urllib.request.urlopen(f"{base_http}{path}", timeout=5) as resp:
            assert resp.status == 200
            ctype = resp.headers.get("Content-Type", "").lower()
            assert (
                "application/javascript" in ctype or "text/javascript" in ctype
            ), f"Invalid JS MIME type for {path}: {ctype}"
            body = resp.read().decode("utf-8")
            if "pcm-processor" in path:
                assert "registerProcessor('pcm16-worklet-processor'" in body

    # 3. Verify WebSocket binary PCM16 48kHz streaming & ping RTT
    async with websockets.connect(f"{base_ws}/ws/audio", compression=None) as ws:
        await ws.send(json.dumps({"type": "ping", "t": 1234.5}))
        pong_raw = await asyncio.wait_for(ws.recv(), timeout=3)
        pong = json.loads(pong_raw)
        assert pong["type"] == "pong"
        assert pong["t"] == 1234.5
        assert pong["block_ms"] == 5.33

        # Stream 20 chunks of 128-frame 440Hz sine wave @ 48kHz Int16 mono (256 bytes each)
        t = np.arange(128, dtype=np.float32) / 48000.0
        sine_pcm16 = (np.sin(2.0 * np.pi * 440.0 * t) * 16000).astype("<i2").tobytes()
        assert len(sine_pcm16) == 256

        for _ in range(20):
            await ws.send(sine_pcm16)

        await asyncio.sleep(0.03)
        with urllib.request.urlopen(f"{base_http}/api/status", timeout=5) as resp:
            status = json.loads(resp.read().decode("utf-8"))
            assert status["connected_clients"] >= 1
            assert status["audio"]["samplerate"] == 48000
            assert status["audio"]["blocksize"] == 256
            assert status["audio"]["rms_dbfs"] > -20.0

        # Mute and verify buffer flush
        await ws.send(json.dumps({"type": "mute", "muted": True}))
        await asyncio.sleep(0.02)
        with urllib.request.urlopen(f"{base_http}/api/status", timeout=5) as resp:
            status_after = json.loads(resp.read().decode("utf-8"))
            assert status_after["audio"]["ring_buffer_ms"] == 0.0


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(UnitPipelineTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
