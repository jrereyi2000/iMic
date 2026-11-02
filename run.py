#!/usr/bin/env python3
"""Entrypoint to launch iMic FastAPI server on 0.0.0.0:8000 with ultra-low-latency settings."""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(
        description="iMic — Ultra-low-latency Smartphone-to-PC Microphone Server"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("IMIC_PORT", "8000")),
        help="Bind port (default: 8000)",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Optional output audio device substring or index (default: 'CABLE Input')",
    )
    args = parser.parse_args()

    os.environ["IMIC_PORT"] = str(args.port)
    if args.device is not None:
        os.environ["IMIC_OUTPUT_DEVICE"] = str(args.device)

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        ws="auto",
        ws_per_message_deflate=False,  # Avoid deflate compression overhead/latency on raw PCM
        ws_max_size=1024 * 64,
        access_log=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
