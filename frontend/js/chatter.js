/* chatter.js — classroom ambience and the mic suppression it needs.
 *
 * When the student first transitions from sentence 1 to sentence 2, we play a
 * short burst of classroom chatter. It's a one-time ritual, deliberately not
 * repeated: on every sentence it becomes a tax the student pays over and over.
 *
 * The mic has to be muted while the tutor is about to speak over the chatter,
 * otherwise the chatter itself feeds into VAD and triggers a phantom barge-in.
 * suppressMic() and releaseMicSuppression() live here because chatter is the
 * only feature that needs them; releasing is safe to call from anywhere.
 *
 * Ducking (duckChatter) is the smooth volume drop when Vidya's first audio
 * chunk arrives — pulled from wherever the chatter is playing, down to a
 * quiet backing track, then faded out entirely a couple of seconds later.
 */

// ── Classroom ambience ───────────────────────────────────────────────────────
/* A quiet room loop under the whole session, the way Disha's clinic ambience
   works. It does one job: remove the dead-silence-between-turns feeling that
   makes a voice agent sound like a phone tree.

   TWO CONSTRAINTS THIS DESIGN IS BUILT AROUND

   1. The mic is always on and the server runs VAD on it. Whatever comes out of
      the speaker gets picked up. Browser echo cancellation uses the output as
      its reference signal so it removes most of this — but AEC is weakest on
      continuous, uncorrelated noise, and it cannot save you if the loop
      contains intelligible speech. USE A LOOP WITHOUT CLEAR DIALOGUE: distant
      murmur, room tone, birds, fan. A recording of people having audible
      conversations will trigger START_SPEECH and Vidya will interrupt herself.

   2. It must duck when she talks. Constant-level ambience competes with her
      voice and makes her harder to understand — which reads as "unprofessional"
      far more than silence does. */

const AMBIENCE_SRC = 'ambience/classroom.mp3';  // supply your own file

// Deliberately very low. This should sit at the edge of perception — if you
// notice it as "audio playing", it's too loud.
const AMBIENCE_LEVEL = 0.05;
const AMBIENCE_DUCKED = 0.02;   // while Vidya is speaking
const AMBIENCE_FADE = 0.8;      // seconds; abrupt starts sound like a glitch

let ambienceGain = null;
let ambienceReady = false;

function startAmbience() {
  if (ambienceReady) return;

  const el = document.getElementById('ambience');
  el.src = AMBIENCE_SRC;
  el.loop = true;

  try {
    const ctx = getPlayContext();
    // Routed through Web Audio (not just the element) so the level can be
    // ramped smoothly instead of stepped.
    const src = ctx.createMediaElementSource(el);
    ambienceGain = ctx.createGain();
    ambienceGain.gain.value = 0;
    src.connect(ambienceGain);
    ambienceGain.connect(ctx.destination);

    el.play().then(() => {
      ambienceReady = true;
      setAmbienceLevel(AMBIENCE_LEVEL);
    }).catch(e => {
      // Autoplay policy: needs a user gesture. startMic() is one, which is why
      // this is called from there.
      console.warn('[ambience] blocked', e);
    });
  } catch (e) {
    console.warn('[ambience] setup failed', e);
  }
}

function setAmbienceLevel(target) {
  if (!ambienceGain) return;
  const ctx = getPlayContext();
  ambienceGain.gain.cancelScheduledValues(ctx.currentTime);
  ambienceGain.gain.setValueAtTime(ambienceGain.gain.value, ctx.currentTime);
  ambienceGain.gain.linearRampToValueAtTime(target, ctx.currentTime + AMBIENCE_FADE);
}

function stopAmbience() {
  if (!ambienceReady) return;
  setAmbienceLevel(0);
  // Let the fade finish before actually pausing, or you hear a click.
  setTimeout(() => {
    try { document.getElementById('ambience').pause(); } catch (e) {}
    ambienceReady = false;
  }, AMBIENCE_FADE * 1000 + 100);
}

// ── Classroom transition ─────────────────────────────────────────────────────
/* A short burst of class chatter when a new sentence comes up, which Vidya
   then settles — the "क्लास... शांत हो जाइए" beat.

   THE PROBLEM THIS SOLVES CAREFULLY: the mic is always on and the server runs
   VAD on it. Chatter is *voices*. Echo cancellation cannot reliably strip a
   crowd, so the server would hear speech, fire START_SPEECH, and Vidya would
   interrupt herself mid-ritual — or worse, transcribe the chatter and answer
   it as if the student had spoken.

   So the mic is muted for the whole cue. The student isn't expected to talk
   over "class, settle down" anyway, which is exactly why this beat works. */

const CHATTER_SRC = 'ambience/classroom-chatter.mp3';

/* The chatter starts on the transition and runs until Vidya speaks over it.
   Its length is NOT fixed — that's the whole point. Every earlier version
   picked an end time and tried to line Vidya up against it, which can't work
   when TTS first-audio swings between 222ms and 1793ms. */
const CHATTER_START = 3.0;   // where in the file the murmur sounds best

/* Safety cap only. The chatter normally ends because Vidya starts speaking,
   not because a timer fired — see duckChatter(). This just stops a turn that
   never produces audio from leaving the room murmuring forever. */
const CHATTER_MAX_MS = 12000;
const CHATTER_LEVEL = 1.0;   // full — this is a deliberate moment, not ambience

/* A hard stop on a waveform mid-cycle produces an audible click. An 80ms
   ramp at the tail removes it without shortening the burst you actually hear. */
const CHATTER_TAIL_FADE = 0.08;

/* Deliberately NOT routed through Web Audio.

   createMediaElementSource() requires the media to be CORS-clean. If the file
   is served from file:// or a different origin, the element still reports a
   successful play() while the graph outputs pure silence — no error, no
   exception, nothing in the console. That failure is indistinguishable from
   "the cue never fired", which is exactly the wrong thing to be debugging.

   A plain HTMLAudioElement with .volume has none of that. We lose the sample
   accurate gain ramp; a short stepped fade is close enough for an 80ms tail. */
let chatterEl = null;
let micSuppressed = false;
let suppressionWatchdog = null;
let chatterStopTimer = null;
let chatterFadeTimer = null;

/* Suppression is normally lifted by audio_end. If that never arrives — TTS
   socket error, dropped turn — the student would be left with a dead mic and
   no way to tell. This guarantees it always comes back. */
const SUPPRESSION_MAX_MS = 12000;

function suppressMic() {
  micSuppressed = true;
  clearTimeout(suppressionWatchdog);
  suppressionWatchdog = setTimeout(() => {
    if (micSuppressed) {
      console.warn('[chatter] no audio_end — releasing mic on watchdog');
      micSuppressed = false;
    }
  }, SUPPRESSION_MAX_MS);
}

/* What happens to the chatter when Vidya starts talking over it.

   She ducks it hard, then it dies away underneath her — the way a class goes
   quiet a beat after the teacher speaks, not the instant she does. This is
   what produces the overlap, and it happens on HER first word, so it works
   regardless of how long TTS took. */
const CHATTER_DUCKED = 0.22;    // level she talks over
const CHATTER_DUCK_MS = 250;    // how fast it drops
const CHATTER_OUTRO_MS = 1400;  // how long it lingers before disappearing

function duckChatter() {
  if (!chatterEl || chatterEl.paused) return;

  clearTimeout(chatterStopTimer);
  clearInterval(chatterFadeTimer);

  const steps = 6;
  const startVol = chatterEl.volume;
  let i = 0;
  chatterFadeTimer = setInterval(() => {
    i++;
    chatterEl.volume = Math.max(
      CHATTER_DUCKED, startVol - (startVol - CHATTER_DUCKED) * (i / steps));
    if (i >= steps) {
      clearInterval(chatterFadeTimer);
      // Now let it drain away under her.
      chatterStopTimer = setTimeout(
        () => fadeOutChatter(CHATTER_OUTRO_MS), 150);
    }
  }, CHATTER_DUCK_MS / steps);
}

function fadeOutChatter(ms) {
  clearInterval(chatterFadeTimer);
  const steps = 8;
  const startVol = chatterEl.volume;
  let i = 0;
  chatterFadeTimer = setInterval(() => {
    i++;
    chatterEl.volume = Math.max(0, startVol * (1 - i / steps));
    if (i >= steps) {
      clearInterval(chatterFadeTimer);
      try { chatterEl.pause(); } catch (e) {}
    }
  }, ms / steps);
}

function playClassChatter() {
  console.log('[chatter] cue fired');
  suppressMic();      // released on audio_end for the tutor's line

  try {
    if (!chatterEl) {
      chatterEl = new Audio(CHATTER_SRC);
      chatterEl.preload = 'auto';
      chatterEl.addEventListener('error', () => {
        const code = chatterEl.error ? chatterEl.error.code : '?';
        console.error(`[chatter] could not load ${CHATTER_SRC} (error code ${code}).`,
                      'Check the file exists at that path and is served over http, not file://');
        micSuppressed = false;
      });
    }

    clearTimeout(chatterStopTimer);
    clearInterval(chatterFadeTimer);

    const startBurst = () => {
      chatterEl.volume = CHATTER_LEVEL;
      chatterEl.playbackRate = 1.0;   // never inherit a slowed rate
      chatterEl.preservesPitch = true;
      chatterEl.currentTime = CHATTER_START;
      chatterEl.play()
        .then(() => console.log(
          `[chatter] playing from ${CHATTER_START}s (dur ${chatterEl.duration}s)`))
        .catch(e => {
          console.warn('[chatter] play blocked —', e.name, e.message);
          micSuppressed = false;
        });

      /* The chatter is NOT stopped on a timer any more.

         Every previous attempt tried to predict when Vidya would start —
         but speak() opens a TTS socket and waits for a first chunk, and that
         has measured anywhere from 222ms to 1793ms. Any fixed offset either
         cuts the noise off before she speaks, or leaves silence in between.

         So the chatter just keeps running. duckChatter() pulls it down the
         instant her first audio chunk plays, and fadeOutChatter() takes it
         away underneath her. The overlap is guaranteed because it's caused by
         her voice rather than timed against it.

         This cap only exists so a turn that never produces audio can't leave
         the room murmuring forever. */
      chatterStopTimer = setTimeout(
        () => fadeOutChatter(CHATTER_TAIL_FADE * 1000), CHATTER_MAX_MS);
    };

    // Seeking before metadata is loaded silently snaps back to 0, so the first
    // cue of a session would play the wrong part of the file.
    if (chatterEl.readyState >= 1) {
      startBurst();
    } else {
      chatterEl.addEventListener('loadedmetadata', startBurst, { once: true });
      chatterEl.load();
    }
  } catch (e) {
    console.warn('[chatter] failed', e);
    micSuppressed = false;   // never leave the mic stuck off
  }
}

function releaseMicSuppression() {
  clearTimeout(suppressionWatchdog);
  micSuppressed = false;
}