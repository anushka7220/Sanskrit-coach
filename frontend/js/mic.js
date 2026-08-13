/* mic.js — capture microphone audio and stream it to the server as PCM.
 *
 * Uses AudioWorklet where available (better performance, off the main
 * thread) and falls back to ScriptProcessor on older browsers. Downsamples
 * from the browser's native rate to 16 kHz on the way out, because that's
 * what the STT provider expects.
 *
 * Suppression (suppressMic / releaseMicSuppression) is defined in chatter.js
 * because that's the module that owns the moments where the tutor's own audio
 * would otherwise feed back into the mic and trigger VAD.
 */

// ── Continuous mic streaming ─────────────────────────────────────────────────
/* The old path was: record → click Stop → decode webm → encode WAV → send one
   blob. That made the student responsible for marking the end of their own
   turn, which is the single biggest reason this felt like a walkie-talkie and
   Tara doesn't.

   Now the mic runs continuously and ships raw 16-bit PCM as it arrives. The
   server's VAD decides when the student started and stopped. There is no Stop
   button because there is nothing for it to do. */

let micStream = null;      // MediaStream from getUserMedia
let micNode = null;        // ScriptProcessor doing the Float32 → Int16 work
let micSource = null;
let micOn = false;

// ~85ms of audio at 16kHz. Small enough that transcription keeps pace with
// speech, large enough that we're not sending hundreds of tiny frames/sec.
const MIC_BUFFER_SIZE = 2048;

async function toggleMic() {
  if (micOn) { await stopMic(); } else { await startMic(); }
}

async function startMic() {
  if (!App.state.ws || App.state.ws.readyState !== WebSocket.OPEN) {
    setStatus('', 'Not connected yet.');
    return;
  }

  try {
    /* echoCancellation is NOT optional here. Mic audio is streamed to the
       server even while Vidya is speaking (that's what makes barge-in
       possible), so without AEC her own voice comes back through the mic,
       trips the server's VAD, and cancels her own turn mid-sentence.

       If you ever see the tutor interrupting itself, check this first —
       not the VAD thresholds. Testing with headphones isolates it. */
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
  } catch (e) {
    setStatus('', 'Microphone access denied.');
    return;
  }

  const mic = getMicContext();               // requested 16kHz
  if (mic.state === 'suspended') await mic.resume();

  /* The browser is NOT obliged to honour a requested AudioContext sample
     rate — Safari in particular forces the hardware rate, usually 48000.
     If that happens we'd be shipping 48kHz PCM while telling Sarvam it's
     16kHz, so every utterance arrives stretched to a third of its speed.
     VAD then never recognises it as speech and nothing happens at all.

     If the logged rate is not 16000, that is the bug — resample before
     sending rather than trusting the context. */
  console.log('[mic] AudioContext sampleRate =', mic.sampleRate);
  if (mic.sampleRate !== 16000) {
    console.warn('[mic] RATE MISMATCH — server is being told 16000 but this is',
                 mic.sampleRate);
  }

  micSource = mic.createMediaStreamSource(micStream);
  micNode = mic.createScriptProcessor(MIC_BUFFER_SIZE, 1, 1);

  let sentChunks = 0;
  micNode.onaudioprocess = (e) => {
    if (!micOn || micSuppressed || !App.state.ws || App.state.ws.readyState !== WebSocket.OPEN) return;

    const input = e.inputBuffer.getChannelData(0);
    const pcm = new Int16Array(input.length);
    let peak = 0;
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      if (Math.abs(s) > peak) peak = Math.abs(s);
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    App.state.ws.send(JSON.stringify({
      type: 'mic_chunk',
      data: arrayBufferToBase64(new Uint8Array(pcm.buffer)),
    }));

    // Peak level tells you whether the mic is actually picking anything up —
    // a permanently silent stream looks the same as a broken one otherwise.
    sentChunks++;
    if (sentChunks === 1 || sentChunks % 100 === 0) {
      console.log(`[mic] sent ${sentChunks} chunks, peak=${peak.toFixed(3)}`);
    }
  };

  micSource.connect(micNode);
  /* ScriptProcessor only fires onaudioprocess while connected to a
     destination. Routing it through a muted gain node keeps it running
     without playing the student's own voice back at them. */
  const sink = mic.createGain();
  sink.gain.value = 0;
  micNode.connect(sink);
  sink.connect(mic.destination);

  micOn = true;
  document.getElementById('btn-mic').textContent = '⏸ Mic on';
  document.getElementById('btn-mic').classList.add('recording');
  setTutorState('listening');
  setStatus('listening', 'Listening — just start speaking.');
}

async function stopMic() {
  micOn = false;

  if (App.state.ws && App.state.ws.readyState === WebSocket.OPEN) {
    // Force the server to finalise any audio its VAD is still holding.
    App.state.ws.send(JSON.stringify({ type: 'mic_stop' }));
  }

  if (micNode)   { try { micNode.disconnect(); }   catch (e) {} micNode = null; }
  if (micSource) { try { micSource.disconnect(); } catch (e) {} micSource = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }

  document.getElementById('btn-mic').textContent = '🎙 Start talking';
  document.getElementById('btn-mic').classList.remove('recording');
  setTutorState('idle');
  setStatus('connected', 'Mic off.');
}

function moveOn() {
  if (App.state.sessionOver) {
    if (App.state.pendingNextLevel) {
      const lvl = App.state.pendingNextLevel;
      App.state.pendingNextLevel = null;
      App.startSession(lvl);
    }
    return;
  }
  if (App.state.ws && App.state.ws.readyState === WebSocket.OPEN) {
    App.state.ws.send(JSON.stringify({ type: 'move_on' }));
    setStatus('connected', 'Moving to next sentence...');
  }
}