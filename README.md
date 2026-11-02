# iMic — Ultra-Low-Latency Smartphone-to-PC Microphone

Turn any smartphone into an ultra-low-latency 48kHz studio microphone for your PC over **USB (ADB Reverse / USB Tethering)** with **Wi-Fi fallback** — no mobile app installation required.

---

## Architecture & Latency Pipeline

- **Frontend (Smartphone Browser):**
  - Vanilla HTML/CSS/JS dark-mode mobile web app using Web Audio API (`AudioContext` configured with `{ latencyHint: 'interactive', sampleRate: 48000 }`).
  - Browser DSP (`echoCancellation`, `noiseSuppression`, `autoGainControl`) **disabled by default** to eliminate 15–40ms of browser audio processing latency (with live UI toggles to re-enable any filter on demand).
  - Custom [`Pcm16WorkletProcessor`](app/static/pcm-processor.js) running in 128-frame render quanta (**~2.67ms** at 48kHz), converting `Float32` samples to little-endian `Int16` PCM and transferring 256-byte `ArrayBuffer` blocks zero-copy over a `TCP_NODELAY` binary WebSocket (`ws_per_message_deflate=False`).
- **Backend (PC — Python 3.12+):**
  - `FastAPI` + `uvicorn` receiving raw 16-bit mono 48kHz PCM binary WebSocket chunks alongside real-time RTT ping telemetry.
  - `sounddevice.RawOutputStream` + `numpy` routed automatically to **`CABLE Input` (VB-Audio Virtual Cable)** (preferring Windows WASAPI for sub-10ms shared-mode output) with `blocksize=256` (**5.33ms**) and `latency='low'`, backed by an auto-trimming bounded ring buffer so audio drift never accumulates.

---

## Quick Start

### 1. Install Virtual Audio Cable (PC)
- **Windows:** Install [VB-Audio Virtual Cable](https://vb-audio.com/Cable/). iMic automatically routes incoming phone audio into **`CABLE Input`**, so you can select **`CABLE Output`** as your microphone in Discord, OBS, Zoom, or games.
- **macOS / Linux:** Install [BlackHole 2ch](https://existential.audio/blackhole/) or PipeWire/PulseAudio virtual sink (or switch target output devices live from the phone UI).

### 2. Install Python Dependencies
Requires **Python 3.12+**:

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Optional: Zero-Latency USB via ADB
1. Enable **Developer Options → USB Debugging** on your Android phone and connect via USB.
2. Either install `adb` on your system `PATH` or extract [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) into `./platform-tools` inside this repository.
3. On startup, iMic automatically executes:
   ```bash
   adb reverse tcp:8000 tcp:8000
   ```
   allowing your phone browser to open **`http://localhost:8000`** directly over USB (treated as a Secure Context by mobile browsers without needing HTTPS certificates).

### 4. Start the Server

```bash
python run.py
# Or directly with uvicorn:
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 5. Connect from Your Phone
- **USB (ADB Reverse — Recommended):** Open `http://localhost:8000` in Chrome on your phone.
- **USB Tethering / RNDIS or Wi-Fi Fallback:** Open one of the local subnet URLs printed in the startup banner (`http://<PC-IP>:8000`).
  > **Note on Non-Localhost HTTP:** Mobile browsers restrict `navigator.mediaDevices.getUserMedia()` on plain HTTP origins other than `localhost`. When using USB Tethering or Wi-Fi IPs over HTTP, add `http://<PC-IP>:8000` to `chrome://flags/#unsafely-treat-insecure-origin-as-secure` in mobile Chrome and relaunch.

---

## Running Verification Tests

```bash
python -m unittest discover -s tests -v
```
