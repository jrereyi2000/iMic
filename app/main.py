"""FastAPI application for iMic ultra-low-latency smartphone microphone."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.audio_engine import AudioOutputEngine
from app.usb_adb import (
    AdbReverseResult,
    NetworkEndpoint,
    discover_network_endpoints,
    print_startup_banner,
    setup_adb_reverse,
)

# Enforce RFC-compliant JavaScript MIME types on all platforms (prevents Windows Registry
# `.js -> text/plain` issues from breaking `AudioWorklet.addModule()`).
mimetypes.init()
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("imic.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("IMIC_PORT", "8000"))

audio_engine = AudioOutputEngine()
adb_state: AdbReverseResult = AdbReverseResult(adb_found=False, port=PORT)
network_endpoints: list[NetworkEndpoint] = []
active_clients: set[WebSocket] = set()


def _enable_tcp_nodelay(websocket: WebSocket) -> None:
    """Disable Nagle's algorithm on the underlying socket for sub-millisecond frame flush."""
    try:
        transport = websocket.scope.get("transport")
        sock = transport.get_extra_info("socket") if transport is not None else None
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize ADB reverse tunnel, scan RNDIS/LAN interfaces, and open PC audio stream."""
    global adb_state, network_endpoints

    adb_state = setup_adb_reverse(port=PORT)
    network_endpoints = discover_network_endpoints(port=PORT)
    audio_engine.start()
    print_startup_banner(adb_state, network_endpoints, audio_engine.summary_line())

    try:
        yield
    finally:
        audio_engine.stop()


app = FastAPI(
    title="iMic",
    description="Ultra-low-latency USB/Wi-Fi smartphone-to-PC microphone server",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", response_class=FileResponse)
async def serve_index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/pcm-processor.js", response_class=FileResponse)
@app.get("/static/pcm-processor.js", response_class=FileResponse)
async def serve_worklet_processor() -> FileResponse:
    """Serve AudioWorkletProcessor with explicit `application/javascript` MIME type."""
    return FileResponse(
        STATIC_DIR / "pcm-processor.js",
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/status")
async def get_status() -> JSONResponse:
    return JSONResponse(
        {
            "connected_clients": len(active_clients),
            "audio": audio_engine.status_dict(),
            "adb": adb_state.to_dict(),
            "endpoints": [asdict(ep) for ep in network_endpoints],
        }
    )


@app.get("/api/devices")
async def get_devices() -> JSONResponse:
    devices = [d.to_dict() for d in audio_engine.list_output_devices()]
    selected_idx = audio_engine.device_info.index if audio_engine.device_info else None
    return JSONResponse({"devices": devices, "selected_index": selected_idx})


@app.post("/api/devices/{device_index}")
async def select_device(device_index: int) -> JSONResponse:
    valid_indices = {d.index for d in audio_engine.list_output_devices()}
    if device_index not in valid_indices:
        raise HTTPException(status_code=404, detail=f"Output device index {device_index} not found")
    status = audio_engine.switch_device(device_index)
    return JSONResponse({"ok": True, "audio": status})


@app.post("/api/usb/reverse")
async def resync_usb() -> JSONResponse:
    """Re-run `adb reverse` and refresh USB Tethering / RNDIS interfaces."""
    global adb_state, network_endpoints
    adb_state = setup_adb_reverse(port=PORT)
    network_endpoints = discover_network_endpoints(port=PORT)
    return JSONResponse(
        {
            "adb": adb_state.to_dict(),
            "endpoints": [asdict(ep) for ep in network_endpoints],
        }
    )


@app.websocket("/ws/audio")
async def websocket_audio_endpoint(websocket: WebSocket) -> None:
    """Receive raw 16-bit 48kHz mono PCM binary chunks and handle low-overhead RTT pings."""
    await websocket.accept()
    _enable_tcp_nodelay(websocket)
    active_clients.add(websocket)
    audio_engine.reset_buffer()

    client_host = websocket.client.host if websocket.client else "unknown"
    logger.info("Phone microphone connected from %s", client_host)

    try:
        while True:
            message: dict[str, Any] = await websocket.receive()
            msg_type = message.get("type")
            if msg_type == "websocket.disconnect":
                break

            # Hot path: raw Int16 PCM mono 48kHz binary frame (128 samples = 256 bytes)
            pcm_bytes = message.get("bytes")
            if pcm_bytes is not None:
                audio_engine.push_pcm16(pcm_bytes)
                continue

            # Control path: lightweight JSON text messages (ping/pong, mute buffer flush)
            text_payload = message.get("text")
            if text_payload is not None:
                try:
                    ctrl = json.loads(text_payload)
                except ValueError:
                    continue

                action = ctrl.get("type")
                if action == "ping":
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "pong",
                                "t": ctrl.get("t", 0),
                                "ring_ms": round(audio_engine.ring.buffered_ms, 2),
                                "block_ms": round(
                                    (audio_engine.blocksize / audio_engine.samplerate) * 1000.0,
                                    2,
                                ),
                                "cable_input": audio_engine.cable_input_found,
                                "device_name": (
                                    audio_engine.device_info.name
                                    if audio_engine.device_info
                                    else "Null Sink"
                                ),
                            }
                        )
                    )
                elif action == "mute" and ctrl.get("muted"):
                    audio_engine.reset_buffer()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket stream closed with error: %s", exc)
    finally:
        active_clients.discard(websocket)
        if not active_clients:
            audio_engine.reset_buffer()
        logger.info("Phone microphone disconnected (%s)", client_host)


app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)
