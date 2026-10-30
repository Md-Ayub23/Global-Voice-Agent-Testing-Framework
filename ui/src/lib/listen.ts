// Listen live: plays the running voice call (caller and agent, as the caller hears it) in the browser.
// The dashboard streams the run's tap datagrams (src/vatf/audio/tap.py), each prefixed by its length.
// Playback is a short jitter buffer; a listener that falls behind skips audio to stay live.

const AHEAD_S = 0.25;       // how far ahead of "now" playback starts
const MAX_BEHIND_S = 1.5;   // more queued than this and new audio is dropped

export class LiveListener {
  private ctx: AudioContext | null = null;
  private abort = new AbortController();
  private next = 0;
  private label = "";

  constructor(private onLabel: (label: string) => void, private onEnd: (error?: string) => void) {}

  async start(jobId: string): Promise<void> {
    this.ctx = new AudioContext();            // created on the click, so the browser lets it play
    let res: Response;
    try {
      res = await fetch(`/api/jobs/${jobId}/listen`, { signal: this.abort.signal, cache: "no-store" });
    } catch (e) {
      return this.finish((e as Error).name === "AbortError" ? undefined : "Couldn't reach the dashboard.");
    }
    if (!res.ok || !res.body) {
      let msg = `listening failed (${res.status})`;
      try { msg = ((await res.json()) as { error?: string }).error || msg; } catch { /* not JSON */ }
      return this.finish(msg);
    }
    const reader = res.body.getReader();
    let buf = new Uint8Array(0);
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const joined = new Uint8Array(buf.length + value.length);
        joined.set(buf); joined.set(value, buf.length);
        buf = joined;
        let at = 0;
        while (buf.length - at >= 4) {
          const n = new DataView(buf.buffer, buf.byteOffset + at, 4).getUint32(0);
          if (buf.length - at - 4 < n) break;
          if (n) this.packet(buf.subarray(at + 4, at + 4 + n));
          at += 4 + n;
        }
        buf = buf.slice(at);
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") return this.finish("The live audio stopped.");
    }
    this.finish();
  }

  private packet(p: Uint8Array) {
    const ctx = this.ctx;
    if (!ctx || p.length < 18) return;
    const dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
    if (String.fromCharCode(p[0], p[1], p[2], p[3]) !== "VTP1") return;
    const sr = dv.getUint32(4);
    const n = dv.getUint16(16);
    const label = new TextDecoder().decode(p.subarray(18, 18 + n));
    const pcm = p.subarray(18 + n);
    const count = pcm.length >> 1;
    if (!count) return;
    if (label !== this.label) {          // a new call: start its audio fresh
      this.label = label;
      this.next = 0;
      this.onLabel(label);
    }
    const now = ctx.currentTime;
    if (this.next < now + 0.02) this.next = now + AHEAD_S;
    if (this.next > now + MAX_BEHIND_S) return;
    const audio = ctx.createBuffer(1, count, sr);
    const ch = audio.getChannelData(0);
    const s = new DataView(pcm.buffer, pcm.byteOffset, count * 2);
    for (let i = 0; i < count; i++) ch[i] = s.getInt16(i * 2, true) / 32768;
    const src = ctx.createBufferSource();
    src.buffer = audio;
    src.connect(ctx.destination);
    src.start(this.next);
    this.next += audio.duration;
  }

  private ended = false;
  private finish(error?: string) {
    if (this.ended) return;
    this.ended = true;
    void this.ctx?.close().catch(() => {});
    this.ctx = null;
    this.onEnd(error);
  }

  stop() {
    this.abort.abort();
    this.finish();
  }
}
