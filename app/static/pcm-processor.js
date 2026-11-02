/**
 * Ultra-low-latency AudioWorkletProcessor (128-frame quantum @ 48kHz = 2.67ms).
 * Converts Float32 [-1.0, 1.0] mono audio directly into little-endian Int16 PCM
 * and transfers the ArrayBuffer zero-copy to the main thread for immediate WS send.
 */
class Pcm16WorkletProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.muted = false;
    this.port.onmessage = (event) => {
      const data = event.data;
      if (data && data.type === 'mute') {
        this.muted = Boolean(data.muted);
      }
    };
  }

  process(inputs) {
    if (this.muted) {
      return true;
    }

    const inputChannels = inputs[0];
    if (!inputChannels || inputChannels.length === 0) {
      return true;
    }

    const channel0 = inputChannels[0];
    const len = channel0.length; // 128 frames per render quantum (~2.67ms @ 48kHz)
    if (len === 0) {
      return true;
    }

    const pcm16 = new Int16Array(len);
    for (let i = 0; i < len; i++) {
      let s = channel0[i];
      if (s > 1.0) s = 1.0;
      else if (s < -1.0) s = -1.0;
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }

    // Zero-copy transfer of the 256-byte ArrayBuffer
    this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    return true;
  }
}

registerProcessor('pcm16-worklet-processor', Pcm16WorkletProcessor);
