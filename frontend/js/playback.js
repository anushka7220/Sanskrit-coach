/* playback.js — how the tutor's voice reaches the speakers.
 *
 * Two paths:
 *   1. Streaming via MediaSource — the fast path. Chunks arrive over the
 *      WebSocket and are appended to a live MP3 stream. Playback starts on
 *      the very first chunk.
 *   2. REST fallback (playFallbackWav) — used when the streaming socket
 *      fails. Slower and one-shot, but always available.
 *
 * The critical detail: barge-in has to be client-authoritative. TTS generates
 * faster than realtime, so the server can think a turn is done while the
 * browser still has seconds buffered. Only the browser knows if sound is
 * actually coming out of the speaker. isPlaying() reads the <audio> element
 * directly rather than tracking a flag that could drift.
 *
 * Two AudioContexts, deliberately — see the comment above getPlayContext.
 */

/* Two AudioContexts, one for capture and one for playback.
   Different native sample rates make one shared context resample audio
   in the wrong direction on the way out. Kept together at the top of
   this file because both playback and mic need them lazily. */
/* Two separate AudioContexts, deliberately.
   The old single context ran at 16 kHz (for mic encoding), which meant 24 kHz
   TTS audio got resampled down on the way out. Recording and playback have
   different native rates, so they get their own contexts. */

App.getPlayContext = function () {
  if (!App.state.playContext)
    App.state.playContext = new AudioContext({ sampleRate: App.PLAYBACK_SAMPLE_RATE });
  return App.state.playContext;
};
App.getMicContext = function () {
  if (!App.state.micContext)
    App.state.micContext = new AudioContext({ sampleRate: App.MIC_SAMPLE_RATE });
  return App.state.micContext;
};

// ── Streaming playback state (MediaSource) ────────────────────────────────────
/* Per-chunk decodeAudioData does NOT work for MP3: every chunk gets decoded
   with its own encoder delay and padding, so joining them leaves silence gaps
   at every boundary — audio comes out broken and stuttery. MediaSource feeds
   the browser one continuous MP3 stream instead, and it handles frame
   boundaries properly. */
let mediaSource = null;
let sourceBuffer = null;
let appendQueue = [];          // chunks waiting for sourceBuffer to be free
let streamEnded = false;       // server sent audio_end
let fallbackSource = null;     // Web Audio node used by the one-shot WAV path
let idleTimer = null;

// Per-turn latency marks
let turnMarkStart = 0;
let gotFirstDelta = false;
let gotFirstAudio = false;

function getPlayContext() {
  if (!App.state.playContext) App.state.playContext = new AudioContext({ sampleRate: App.PLAYBACK_SAMPLE_RATE });
  return App.state.playContext;
}
function getMicContext() {
  if (!App.state.micContext) App.state.micContext = new AudioContext({ sampleRate: App.MIC_SAMPLE_RATE });
  return App.state.micContext;
}

function b64ToBytes(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/* Start a fresh MediaSource for a new utterance.

   One MediaSource per turn. Tearing it down and rebuilding is the cleanest
   way to guarantee no audio from a previous turn leaks into this one — which
   is exactly what barge-in will need. */
function startAudioStream() {
  const audio = document.getElementById('tts-audio');

  // Tear down anything from the previous turn.
  try { audio.pause(); } catch (e) {}
  if (mediaSource && mediaSource.readyState === 'open') {
    try { mediaSource.endOfStream(); } catch (e) {}
  }
  if (audio.src) {
    try { URL.revokeObjectURL(audio.src); } catch (e) {}
  }

  appendQueue = [];
  streamEnded = false;
  sourceBuffer = null;

  mediaSource = new MediaSource();
  audio.src = URL.createObjectURL(mediaSource);

  mediaSource.addEventListener('sourceopen', () => {
    try {
      sourceBuffer = mediaSource.addSourceBuffer('audio/mpeg');
    } catch (e) {
      console.error('[Audio] audio/mpeg not supported by MediaSource', e);
      return;
    }
    sourceBuffer.addEventListener('updateend', pumpAppendQueue);
    pumpAppendQueue();
  }, { once: true });

  audio.addEventListener('ended', () => {
    setTutorState('idle');
    document.getElementById('ai-cloud').classList.remove('speaking');
    setStatus('connected', 'Your turn — speak or ask a question.');
    if (App.state.ws && App.state.ws.readyState === WebSocket.OPEN) {
      App.state.ws.send(JSON.stringify({ type: 'playback_finished' }));
    }
  }, { once: true });
}

/* SourceBuffer can only take one append at a time, so chunks queue up and
   drain on each 'updateend'. */
function pumpAppendQueue() {
  if (!sourceBuffer || sourceBuffer.updating) return;

  if (appendQueue.length) {
    const chunk = appendQueue.shift();
    try {
      sourceBuffer.appendBuffer(chunk);
    } catch (e) {
      console.warn('[Audio] appendBuffer failed', e);
    }
    return;
  }

  // Queue drained and the server says there's nothing more coming.
  if (streamEnded && mediaSource && mediaSource.readyState === 'open') {
    try { mediaSource.endOfStream(); } catch (e) {}
  }
}

function scheduleChunk(b64) {
  if (!mediaSource) startAudioStream();
  appendQueue.push(b64ToBytes(b64));
  pumpAppendQueue();

  if (!gotFirstAudio) {
    gotFirstAudio = true;
    mark('m-audio');
    setTutorState('speaking');
    setStatus('processing', 'Vidya is speaking...');
    // If the classroom chatter is still running, pull it down under her.
    duckChatter();
    const audio = document.getElementById('tts-audio');
    audio.play().catch(e => console.warn('[Audio] play() blocked', e));
  }
}

/* Instant stop. This is the entire client half of barge-in — call it the
   moment STT reports speech_start (step 3). */
/* The single source of truth for "is Vidya audible right now". Derived from
   the audio element rather than a flag we maintain, so it can't drift out of
   sync with what the user actually hears. */
function isPlaying() {
  const audio = document.getElementById('tts-audio');
  if (!audio || !audio.src) return false;
  return !audio.paused && !audio.ended && audio.currentTime > 0;
}

function stopPlayback() {
  const audio = document.getElementById('tts-audio');
  try { audio.pause(); } catch (e) {}
  appendQueue = [];
  streamEnded = false;
  if (mediaSource && mediaSource.readyState === 'open') {
    try { mediaSource.endOfStream(); } catch (e) {}
  }
  /* Dropping the reference isn't enough — the object URL keeps the
     MediaSource and its buffered audio alive. Barge-in calls this on every
     interruption, so without the revoke a long session accumulates one
     orphaned MediaSource per turn. */
  if (audio.src) {
    try { URL.revokeObjectURL(audio.src); } catch (e) {}
    audio.removeAttribute('src');
  }
  mediaSource = null;
  sourceBuffer = null;
  // The REST fallback plays through Web Audio, so kill that too.
  if (fallbackSource) {
    try { fallbackSource.stop(); } catch (e) {}
    fallbackSource = null;
  }
  clearTimeout(idleTimer);
}

/* Audio is buffered ahead of the playhead, so "the server stopped sending" is
   not "the tutor stopped talking". Signal end-of-stream and let the audio
   element's 'ended' event flip the UI back to idle. */
function scheduleIdleAfterPlayback() {
  streamEnded = true;
  pumpAppendQueue();
}

function mark(elementId) {
  if (!turnMarkStart) return;
  const ms = Math.round(performance.now() - turnMarkStart);
  document.getElementById(elementId).textContent = ms + 'ms';
}


// ── WAV encoder ───────────────────────────────────────────────────────────────
function encodeWAV(audioBuffer) {
  const numChannels = 1;
  const sampleRate = audioBuffer.sampleRate;
  const samples = audioBuffer.getChannelData(0);
  const int16 = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    int16[i] = Math.max(-32768, Math.min(32767, Math.round(samples[i] * 32767)));
  }
  const dataLength = int16.length * 2;
  const buffer = new ArrayBuffer(44 + dataLength);
  const view = new DataView(buffer);

  const writeStr = (off, str) => { for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + dataLength, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * numChannels * 2, true);
  view.setUint16(32, numChannels * 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, 'data');
  view.setUint32(40, dataLength, true);
  new Int16Array(buffer, 44).set(int16);
  return buffer;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

// ── Fallback playback (self-describing WAV, one shot) ────────────────────────
/* Used when the backend couldn't open the TTS socket and sent a single WAV.
   A complete WAV is self-describing, so plain decodeAudioData is fine here —
   the per-chunk decode problem only applies to split MP3 streams. */
async function playBase64Wav(b64) {
  try {
    const bytes = b64ToBytes(b64);
    const ctx = getPlayContext();
    if (ctx.state === 'suspended') await ctx.resume();

    const buffer = await ctx.decodeAudioData(bytes.buffer.slice(0));
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    source.start(0);
    fallbackSource = source;
    setTutorState('speaking');
    source.onended = () => {
      fallbackSource = null;
      setTutorState('idle');
      document.getElementById('ai-cloud').classList.remove('speaking');
      setStatus('connected', 'Your turn — speak or ask a question.');
      if (App.state.ws && App.state.ws.readyState === WebSocket.OPEN) {
        App.state.ws.send(JSON.stringify({ type: 'playback_finished' }));
      }
    };
  } catch (e) {
    console.warn('[Audio] Playback failed:', e);
    setTutorState('idle');
    setStatus('connected', 'Audio playback failed — but you can read the reply above.');
  }
}



// ── Exports used cross-module via App.* ──────────────────────────────────
App.getPlayContext = getPlayContext;
App.getMicContext = getMicContext;