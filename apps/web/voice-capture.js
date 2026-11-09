class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.samples = new Int16Array(640);
    this.position = 0;
  }

  process(inputs) {
    const mono = inputs[0]?.[0];
    if (!mono) return true;
    for (const value of mono) {
      this.sum += value;
      this.count++;
      this.phase += 16000;
      if (this.phase < sampleRate) continue;
      this.phase -= sampleRate;
      const sample = Math.max(-1, Math.min(1, this.sum / this.count));
      this.samples[this.position++] = Math.round(sample * 32767);
      this.sum = 0;
      this.count = 0;
      if (this.position === this.samples.length) {
        this.port.postMessage(this.samples.buffer, [this.samples.buffer]);
        this.samples = new Int16Array(640);
        this.position = 0;
      }
    }
    return true;
  }
}

registerProcessor("voice-capture", VoiceCapture);
