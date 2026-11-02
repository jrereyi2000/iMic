(() => {
  'use strict';

  const TARGET_SAMPLE_RATE = 48000;
  const MAX_WS_BUFFERED_BYTES = 8192; // Drop stale frames if network stalls (~85ms cap)
  const PING_INTERVAL_MS = 500;

  // DOM Elements
  const connDot = document.getElementById('connDot');
  const transportModeLabel = document.getElementById('transportModeLabel');
  const pingDot = document.getElementById('pingDot');
  const pingValue = document.getElementById('pingValue');
  const insecureBanner = document.getElementById('insecureBanner');
  const dbReadout = document.getElementById('dbReadout');
  const canvas = document.getElementById('visualizerCanvas');
  const canvasCtx = canvas.getContext('2d');
  const meterFill = document.getElementById('meterFill');
  const quantumStat = document.getElementById('quantumStat');
  const pcBufferStat = document.getElementById('pcBufferStat');
  const pcTargetStat = document.getElementById('pcTargetStat');
  const startStopBtn = document.getElementById('startStopBtn');
  const startStopLabel = document.getElementById('startStopLabel');
  const muteBtn = document.getElementById('muteBtn');
  const muteLabel = document.getElementById('muteLabel');
  const gainSlider = document.getElementById('gainSlider');
  const gainValue = document.getElementById('gainValue');
  const resetGainBtn = document.getElementById('resetGainBtn');
  const toggleEcho = document.getElementById('toggleEcho');
  const toggleNoise = document.getElementById('toggleNoise');
  const toggleAgc = document.getElementById('toggleAgc');
  const phoneMicSelect = document.getElementById('phoneMicSelect');
  const pcDeviceSelect = document.getElementById('pcDeviceSelect');
  const statusFooter = document.getElementById('statusFooter');

  // Runtime State
  let ws = null;
  let pingTimer = null;
  let reconnectTimer = null;
  let smoothedRttMs = null;

  let audioCtx = null;
  let mediaStream = null;
  let sourceNode = null;
  let gainNode = null;
  let analyserNode = null;
  let workletNode = null;
  let wakeLock = null;

  let isStreaming = false;
  let isMuted = false;
  let currentGain = 1.0;

  // Visualizer buffers
  const BAR_COUNT = 44;
  const smoothedBars = new Float32Array(BAR_COUNT);
  let timeDomainBuf = new Float32Array(256);
  let freqDomainBuf = new Uint8Array(128);

  // Detect transport hint from URL
  function updateTransportLabel() {
    const host = window.location.hostname;
    if (host === 'localhost' || host === '127.0.0.1') {
      transportModeLabel.textContent = 'USB ADB Tunnel · 48kHz PCM16';
    } else if (host.startsWith('192.168.42.') || host.startsWith('172.20.10.')) {
      transportModeLabel.textContent = 'USB Tethering · 48kHz PCM16';
    } else {
      transportModeLabel.textContent = `Wi-Fi / LAN (${host}) · 48kHz PCM16`;
    }

    if (!window.isSecureContext && !navigator.mediaDevices) {
      insecureBanner.classList.remove('hidden');
    }
  }

  // Format gain percentage + dB
  function updateGainDisplay(percent) {
    currentGain = percent / 100;
    if (percent === 0) {
      gainValue.textContent = '0% (-∞ dB)';
    } else {
      const db = 20 * Math.log10(currentGain);
      const sign = db >= 0 ? '+' : '';
      gainValue.textContent = `${percent}% (${sign}${db.toFixed(1)} dB)`;
    }
    if (gainNode && audioCtx) {
      const target = isMuted ? 0 : currentGain;
      gainNode.gain.setTargetAtTime(target, audioCtx.currentTime, 0.008);
    }
  }

  // Establish persistent binary+control WebSocket
  function connectWebSocket() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${window.location.host}/ws/audio`;
    ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      startPingLoop();
      updateUiStatus();
    };

    ws.onmessage = (event) => {
      if (typeof event.data !== 'string') return;
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'pong' && typeof msg.t === 'number') {
          const rtt = Math.max(0.1, performance.now() - msg.t);
          smoothedRttMs = smoothedRttMs === null ? rtt : smoothedRttMs * 0.65 + rtt * 0.35;
          renderPing(smoothedRttMs);

          if (typeof msg.ring_ms === 'number' && typeof msg.block_ms === 'number') {
            pcBufferStat.textContent = `${(msg.ring_ms + msg.block_ms).toFixed(1)} ms`;
          }
          if (msg.device_name) {
            pcTargetStat.textContent = msg.cable_input ? 'CABLE Input' : msg.device_name;
          }
        }
      } catch {
        // Ignore malformed control frames
      }
    };

    ws.onclose = () => {
      stopPingLoop();
      smoothedRttMs = null;
      pingValue.textContent = 'Offline';
      pingDot.className = 'ping-dot bad';
      updateUiStatus();
      reconnectTimer = setTimeout(connectWebSocket, 1200);
    };

    ws.onerror = () => {
      // Handled by onclose retry
    };
  }

  function startPingLoop() {
    stopPingLoop();
    const sendPing = () => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping', t: performance.now() }));
      }
    };
    sendPing();
    pingTimer = setInterval(sendPing, PING_INTERVAL_MS);
  }

  function stopPingLoop() {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  }

  function renderPing(rttMs) {
    pingValue.textContent = `${rttMs < 10 ? rttMs.toFixed(1) : Math.round(rttMs)} ms`;
    if (rttMs < 8) {
      pingDot.className = 'ping-dot good';
    } else if (rttMs < 25) {
      pingDot.className = 'ping-dot warn';
    } else {
      pingDot.className = 'ping-dot bad';
    }
  }

  // Audio Constraints (DSP disabled by default for ultra-low latency)
  function buildAudioConstraints() {
    const constraints = {
      echoCancellation: toggleEcho.checked,
      noiseSuppression: toggleNoise.checked,
      autoGainControl: toggleAgc.checked,
      channelCount: { ideal: 1 },
      sampleRate: { ideal: TARGET_SAMPLE_RATE },
      latency: { ideal: 0 },
    };
    if (phoneMicSelect.value) {
      constraints.deviceId = { exact: phoneMicSelect.value };
    }
    return constraints;
  }

  async function requestWakeLock() {
    if ('wakeLock' in navigator) {
      try {
        wakeLock = await navigator.wakeLock.request('screen');
      } catch {
        // Non-fatal if battery saver blocks wakeLock
      }
    }
  }

  async function releaseWakeLock() {
    if (wakeLock) {
      try {
        await wakeLock.release();
      } catch {
        // Ignore
      }
      wakeLock = null;
    }
  }

  async function refreshPhoneMicrophones() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const mics = devices.filter((d) => d.kind === 'audioinput');
      if (mics.length === 0) return;

      const currentVal = phoneMicSelect.value;
      phoneMicSelect.innerHTML = '';
      mics.forEach((mic, idx) => {
        const opt = document.createElement('option');
        opt.value = mic.deviceId;
        opt.textContent = mic.label || `Microphone ${idx + 1}`;
        phoneMicSelect.appendChild(opt);
      });
      if (currentVal && [...phoneMicSelect.options].some((o) => o.value === currentVal)) {
        phoneMicSelect.value = currentVal;
      }
    } catch {
      // Ignore permission errors prior to grant
    }
  }

  async function refreshPcOutputDevices() {
    try {
      const res = await fetch('/api/devices');
      if (!res.ok) return;
      const data = await res.json();
      pcDeviceSelect.innerHTML = '';
      if (!data.devices || data.devices.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No PC output devices detected';
        pcDeviceSelect.appendChild(opt);
        pcTargetStat.textContent = 'Null Sink';
        return;
      }

      data.devices.forEach((dev) => {
        const opt = document.createElement('option');
        opt.value = String(dev.index);
        const badge = dev.is_cable_input ? '★ ' : '';
        opt.textContent = `${badge}${dev.name} (${dev.hostapi})`;
        if (dev.index === data.selected_index) {
          opt.selected = true;
          pcTargetStat.textContent = dev.is_cable_input ? 'CABLE Input' : dev.name;
        }
        pcDeviceSelect.appendChild(opt);
      });
    } catch {
      pcTargetStat.textContent = 'Offline';
    }
  }

  // Start Ultra-Low-Latency Audio Pipeline
  async function startStreaming() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      insecureBanner.classList.remove('hidden');
      statusFooter.textContent = 'Error: Microphone API requires localhost (ADB Reverse) or HTTPS.';
      return;
    }

    try {
      startStopBtn.disabled = true;
      statusFooter.textContent = 'Opening 48kHz interactive audio pipeline…';

      connectWebSocket();

      // 1. Acquire raw low-latency microphone stream (DSP off by default)
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: buildAudioConstraints(),
        video: false,
      });

      await refreshPhoneMicrophones();

      // 2. Configure AudioContext with interactive latencyHint & 48kHz sample rate
      const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
      audioCtx = new AudioContextCtor({
        latencyHint: 'interactive',
        sampleRate: TARGET_SAMPLE_RATE,
      });
      if (audioCtx.state === 'suspended') {
        await audioCtx.resume();
      }

      // 3. Load AudioWorkletProcessor (128-frame quantum -> Int16 PCM)
      await audioCtx.audioWorklet.addModule('/pcm-processor.js');

      sourceNode = audioCtx.createMediaStreamSource(mediaStream);
      gainNode = audioCtx.createGain();
      gainNode.gain.value = isMuted ? 0 : currentGain;

      analyserNode = audioCtx.createAnalyser();
      analyserNode.fftSize = 256;
      analyserNode.smoothingTimeConstant = 0.55;
      timeDomainBuf = new Float32Array(analyserNode.fftSize);
      freqDomainBuf = new Uint8Array(analyserNode.frequencyBinCount);

      workletNode = new AudioWorkletNode(audioCtx, 'pcm16-worklet-processor', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      workletNode.port.postMessage({ type: 'mute', muted: isMuted });

      workletNode.port.onmessage = (event) => {
        if (
          !isMuted &&
          ws &&
          ws.readyState === WebSocket.OPEN &&
          ws.bufferedAmount < MAX_WS_BUFFERED_BYTES
        ) {
          ws.send(event.data);
        }
      };

      // Connect graph (worklet outputs silence to destination to guarantee continuous pull)
      sourceNode.connect(gainNode);
      gainNode.connect(analyserNode);
      gainNode.connect(workletNode);
      workletNode.connect(audioCtx.destination);

      const baseLatMs = typeof audioCtx.baseLatency === 'number'
        ? (audioCtx.baseLatency * 1000).toFixed(1)
        : '2.7';
      quantumStat.textContent = `128f (${baseLatMs}ms)`;

      isStreaming = true;
      muteBtn.disabled = false;
      await requestWakeLock();
      updateUiStatus();
    } catch (err) {
      await stopStreaming();
      statusFooter.textContent = `Mic error: ${err && err.message ? err.message : String(err)}`;
    } finally {
      startStopBtn.disabled = false;
    }
  }

  async function stopStreaming() {
    isStreaming = false;
    isMuted = false;

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'mute', muted: true }));
    }

    if (workletNode) {
      workletNode.port.onmessage = null;
      try {
        workletNode.disconnect();
      } catch {}
      workletNode = null;
    }
    if (analyserNode) {
      try {
        analyserNode.disconnect();
      } catch {}
      analyserNode = null;
    }
    if (gainNode) {
      try {
        gainNode.disconnect();
      } catch {}
      gainNode = null;
    }
    if (sourceNode) {
      try {
        sourceNode.disconnect();
      } catch {}
      sourceNode = null;
    }
    if (mediaStream) {
      mediaStream.getTracks().forEach((track) => track.stop());
      mediaStream = null;
    }
    if (audioCtx) {
      try {
        await audioCtx.close();
      } catch {}
      audioCtx = null;
    }

    await releaseWakeLock();
    muteBtn.disabled = true;
    muteBtn.classList.remove('is-muted');
    muteLabel.textContent = 'Mute';
    dbReadout.textContent = '-∞ dB';
    meterFill.style.width = '0%';
    updateUiStatus();
  }

  // Apply updated DSP or mic capsule constraints on the fly
  async function updateLiveStreamConstraints() {
    if (!isStreaming || !mediaStream || !audioCtx || !gainNode) return;

    const constraints = buildAudioConstraints();
    const [track] = mediaStream.getAudioTracks();

    // If only DSP flags changed on the same device, try in-place applyConstraints first
    if (track && !phoneMicSelect.dataset.switchingDevice) {
      try {
        await track.applyConstraints({
          echoCancellation: constraints.echoCancellation,
          noiseSuppression: constraints.noiseSuppression,
          autoGainControl: constraints.autoGainControl,
        });
        const applied = track.getSettings ? track.getSettings() : {};
        if (
          applied.echoCancellation === constraints.echoCancellation &&
          applied.noiseSuppression === constraints.noiseSuppression &&
          applied.autoGainControl === constraints.autoGainControl
        ) {
          statusFooter.textContent = 'Updated browser DSP settings live';
          return;
        }
      } catch {
        // Fall through to clean stream swap
      }
    }

    // Re-open stream and hot-swap sourceNode without interrupting AudioContext or WebSocket
    try {
      const nextStream = await navigator.mediaDevices.getUserMedia({
        audio: constraints,
        video: false,
      });
      const nextSource = audioCtx.createMediaStreamSource(nextStream);
      if (sourceNode) {
        try {
          sourceNode.disconnect();
        } catch {}
      }
      mediaStream.getTracks().forEach((t) => t.stop());
      mediaStream = nextStream;
      sourceNode = nextSource;
      sourceNode.connect(gainNode);
      statusFooter.textContent = 'Switched audio input / DSP profile cleanly';
    } catch (err) {
      statusFooter.textContent = `Constraint update failed: ${err.message || err}`;
    }
  }

  function toggleMute() {
    if (!isStreaming) return;
    isMuted = !isMuted;
    if (navigator.vibrate) {
      navigator.vibrate(15);
    }

    if (gainNode && audioCtx) {
      gainNode.gain.setTargetAtTime(isMuted ? 0 : currentGain, audioCtx.currentTime, 0.006);
    }
    if (workletNode) {
      workletNode.port.postMessage({ type: 'mute', muted: isMuted });
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'mute', muted: isMuted }));
    }

    muteBtn.classList.toggle('is-muted', isMuted);
    muteLabel.textContent = isMuted ? 'Unmute' : 'Mute';
    updateUiStatus();
  }

  function updateUiStatus() {
    const wsConnected = ws && ws.readyState === WebSocket.OPEN;

    if (isStreaming) {
      startStopBtn.classList.add('streaming');
      startStopLabel.textContent = 'Stop Mic';
      if (isMuted) {
        connDot.className = 'brand-dot muted';
        statusFooter.textContent = 'MUTED · PC buffer flushed (0 output)';
      } else if (wsConnected) {
        connDot.className = 'brand-dot live';
        statusFooter.textContent = 'LIVE · Streaming 48kHz 16-bit PCM mono over WebSocket';
      } else {
        connDot.className = 'brand-dot';
        statusFooter.textContent = 'Reconnecting WebSocket to PC…';
      }
    } else {
      startStopBtn.classList.remove('streaming');
      startStopLabel.textContent = 'Start Microphone';
      connDot.className = 'brand-dot';
      statusFooter.textContent = wsConnected
        ? 'Connected to PC · Tap "Start Microphone" to stream'
        : 'Connecting to PC server…';
    }
  }

  // High-DPI Canvas Resizing & 60fps Volume Visualizer
  function resizeCanvas() {
    const rect = canvas.parentElement.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  }

  function renderVisualizerFrame() {
    const w = canvas.width;
    const h = canvas.height;
    canvasCtx.clearRect(0, 0, w, h);

    // Subtle center reference line
    canvasCtx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
    canvasCtx.lineWidth = 1;
    canvasCtx.beginPath();
    canvasCtx.moveTo(0, h / 2);
    canvasCtx.lineTo(w, h / 2);
    canvasCtx.stroke();

    if (isStreaming && analyserNode && !isMuted) {
      analyserNode.getFloatTimeDomainData(timeDomainBuf);
      analyserNode.getByteFrequencyData(freqDomainBuf);

      // Compute RMS dBFS for digital readout & meter bar
      let sumSq = 0;
      for (let i = 0; i < timeDomainBuf.length; i++) {
        const s = timeDomainBuf[i];
        sumSq += s * s;
      }
      const rms = Math.sqrt(sumSq / timeDomainBuf.length);
      const db = 20 * Math.log10(Math.max(rms, 1e-5));
      dbReadout.textContent = db <= -65 ? '-∞ dB' : `${db.toFixed(1)} dB`;

      const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
      meterFill.style.width = `${pct.toFixed(1)}%`;

      // Render symmetric studio spectrum bars
      const gap = Math.max(2, Math.floor(w * 0.006));
      const totalGap = gap * (BAR_COUNT - 1);
      const barWidth = Math.max(2, (w - totalGap - 16) / BAR_COUNT);
      const startX = (w - (barWidth * BAR_COUNT + totalGap)) / 2;

      for (let i = 0; i < BAR_COUNT; i++) {
        const bin = Math.min(freqDomainBuf.length - 1, Math.floor((i / BAR_COUNT) * 72));
        const norm = freqDomainBuf[bin] / 255;
        smoothedBars[i] = smoothedBars[i] * 0.62 + norm * 0.38;

        const barH = Math.max(3, smoothedBars[i] * (h * 0.84));
        const x = startX + i * (barWidth + gap);
        const y = (h - barH) / 2;

        if (smoothedBars[i] > 0.85) {
          canvasCtx.fillStyle = '#ef4444';
        } else if (smoothedBars[i] > 0.6) {
          canvasCtx.fillStyle = '#22d3ee';
        } else {
          canvasCtx.fillStyle = '#10b981';
        }

        canvasCtx.beginPath();
        if (canvasCtx.roundRect) {
          canvasCtx.roundRect(x, y, barWidth, barH, barWidth / 2);
        } else {
          canvasCtx.rect(x, y, barWidth, barH);
        }
        canvasCtx.fill();
      }
    } else {
      // Idle or muted minimal bars
      if (isMuted) {
        dbReadout.textContent = 'MUTED';
        meterFill.style.width = '0%';
      }
      const gap = Math.max(2, Math.floor(w * 0.006));
      const totalGap = gap * (BAR_COUNT - 1);
      const barWidth = Math.max(2, (w - totalGap - 16) / BAR_COUNT);
      const startX = (w - (barWidth * BAR_COUNT + totalGap)) / 2;
      canvasCtx.fillStyle = isMuted ? 'rgba(239, 68, 68, 0.32)' : 'rgba(139, 149, 171, 0.2)';
      for (let i = 0; i < BAR_COUNT; i++) {
        smoothedBars[i] *= 0.82;
        const barH = Math.max(3, smoothedBars[i] * (h * 0.84));
        const x = startX + i * (barWidth + gap);
        const y = (h - barH) / 2;
        canvasCtx.fillRect(x, y, barWidth, barH);
      }
    }

    requestAnimationFrame(renderVisualizerFrame);
  }

  // Event Listeners
  startStopBtn.addEventListener('click', () => {
    if (isStreaming) {
      stopStreaming();
    } else {
      startStreaming();
    }
  });

  muteBtn.addEventListener('click', toggleMute);

  gainSlider.addEventListener('input', (e) => {
    updateGainDisplay(Number(e.target.value));
  });

  resetGainBtn.addEventListener('click', () => {
    gainSlider.value = '100';
    updateGainDisplay(100);
  });

  [toggleEcho, toggleNoise, toggleAgc].forEach((el) => {
    el.addEventListener('change', () => {
      updateLiveStreamConstraints();
    });
  });

  phoneMicSelect.addEventListener('change', async () => {
    phoneMicSelect.dataset.switchingDevice = '1';
    try {
      await updateLiveStreamConstraints();
    } finally {
      delete phoneMicSelect.dataset.switchingDevice;
    }
  });

  pcDeviceSelect.addEventListener('change', async () => {
    const idx = pcDeviceSelect.value;
    if (idx === '') return;
    try {
      const res = await fetch(`/api/devices/${encodeURIComponent(idx)}`, { method: 'POST' });
      if (res.ok) {
        const payload = await res.json();
        const dev = payload.audio && payload.audio.device;
        if (dev) {
          pcTargetStat.textContent = dev.is_cable_input ? 'CABLE Input' : dev.name;
          statusFooter.textContent = `Routed PC output to ${dev.name}`;
        }
      }
    } catch {
      statusFooter.textContent = 'Failed switching PC output device';
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && isStreaming) {
      requestWakeLock();
      if (audioCtx && audioCtx.state === 'suspended') {
        audioCtx.resume();
      }
    }
  });

  if (window.ResizeObserver) {
    new ResizeObserver(resizeCanvas).observe(canvas.parentElement);
  } else {
    window.addEventListener('resize', resizeCanvas);
  }

  // Boot
  updateTransportLabel();
  updateGainDisplay(100);
  resizeCanvas();
  connectWebSocket();
  refreshPhoneMicrophones();
  refreshPcOutputDevices();
  requestAnimationFrame(renderVisualizerFrame);
})();
