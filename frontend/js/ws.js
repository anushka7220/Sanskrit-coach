/* ws.js — the WebSocket that carries every message except the initial POST.
 *
 * Owns:
 *   - connectWS: opens the socket and sends init
 *   - reconnect: with exponential backoff, unless the close was deliberate
 *   - heartbeat: ping every 15s; if the server goes silent for 45s, force a
 *     reconnect (a live-looking socket can still be dead)
 *   - message routing: the big switch that decides what each server message
 *     means for the UI
 *
 * Everything that reads or writes App.state.ws goes through here.
 */

// ── WebSocket ─────────────────────────────────────────────────────────────────
/* A dropped socket must not end the lesson. Code 1001 ("going away") shows up
   whenever the browser freezes a backgrounded tab or the machine dozes, which
   is easy to hit now that the mic streams continuously — so reconnecting is
   normal operation, not error handling. */
let reconnectAttempts = 0;
let reconnectTimer = null;
let heartbeatTimer = null;
let lastServerMsgAt = 0;
const MAX_RECONNECT_ATTEMPTS = 5;

/* Why a heartbeat when the mic is already streaming constantly:

   Mic traffic only proves the CLIENT is alive. If the server side of the
   connection has gone away — proxy timeout, worker restart, laptop waking from
   sleep — the browser can keep writing into a socket that is already dead and
   report nothing for a long time. The ping/pong makes the failure visible in
   seconds instead of whenever the next send happens to error. */
const HEARTBEAT_MS = 15000;
const SERVER_SILENCE_LIMIT_MS = 45000;

function startHeartbeat() {
  clearInterval(heartbeatTimer);
  lastServerMsgAt = Date.now();
  heartbeatTimer = setInterval(() => {
    if (!App.state.ws || App.state.ws.readyState !== WebSocket.OPEN) return;

    if (Date.now() - lastServerMsgAt > SERVER_SILENCE_LIMIT_MS) {
      console.warn('[WS] no response from server — forcing reconnect');
      try { App.state.ws.close(4000, 'heartbeat timeout'); } catch (e) {}
      return;   // onclose schedules the reconnect
    }
    try { App.state.ws.send(JSON.stringify({ type: 'ping' })); } catch (e) {}
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  clearInterval(heartbeatTimer);
}

/* Backgrounded tabs get frozen, and a frozen tab's socket is often closed by
   the browser (this is the code 1001 you kept seeing). Timers are throttled
   while hidden, so the reconnect may not have run — check the moment the tab
   comes back. */
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;

  /* CRITICAL: clear the flag first.

     beforeunload does NOT only fire on navigation — the browser also fires it
     when it freezes or discards a backgrounded tab. That set App.state.deliberateClose
     to true, and scheduleReconnect() returns early on it, so once a tab had
     been backgrounded even once, reconnection was dead for the rest of the
     session. The page being visible again proves it wasn't a real unload. */
  App.state.deliberateClose = false;

  if (!App.state.sessionId) return;
  if (!App.state.ws || App.state.ws.readyState === WebSocket.CLOSED) {
    console.log('[WS] tab visible again, socket is closed — reconnecting');
    reconnectAttempts = 0;
    connectWS(App.state.sessionId, App.state.currentLevel);
  }
});

/* Restored from the back/forward cache. Same reasoning as above: the page is
   alive, so nothing was deliberately closed. */
window.addEventListener('pageshow', (e) => {
  if (!e.persisted) return;
  App.state.deliberateClose = false;
  if (App.state.sessionId && (!App.state.ws || App.state.ws.readyState === WebSocket.CLOSED)) {
    reconnectAttempts = 0;
    connectWS(App.state.sessionId, App.state.currentLevel);
  }
});

function connectWS(sid, level) {
  clearTimeout(reconnectTimer);
  App.state.ws = new WebSocket(`${App.WS_BASE}/ws/${sid}`);

  App.state.ws.onopen = () => {
    reconnectAttempts = 0;
    startHeartbeat();
    setStatus('connected', 'Connected — sending init...');
    // Resume where the student actually is. Sending 0 would silently restart
    // the lesson from the first sentence after any blip.
    App.state.ws.send(JSON.stringify({
      type: 'init',
      level,
      sentence_index: App.state.currentIndex || 0,
      // Resent on every reconnect — the server holds this per connection, so
      // dropping it would make Vidya forget the student's name mid-lesson.
      name: App.state.studentName || '',
      gender: App.state.studentGender || 'neutral',
    }));
  };

  function scheduleReconnect() {
    if (App.state.deliberateClose) return;
    if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
      setStatus('', 'Disconnected. Reload the page to continue.');
      return;
    }
    // Back off so a server that's actually down isn't hammered.
    const delay = Math.min(1000 * 2 ** reconnectAttempts, 8000);
    reconnectAttempts++;
    setStatus('', `Reconnecting (${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})...`);
    reconnectTimer = setTimeout(() => connectWS(sid, level), delay);
  }
  App.state.ws._scheduleReconnect = scheduleReconnect;

  App.state.ws.onmessage = async (event) => {
    lastServerMsgAt = Date.now();   // any traffic proves the server is alive
    const msg = JSON.parse(event.data);
    if (msg.type === 'pong') return;

    switch (msg.type) {

      case 'next_sentence':
        // Only the first transition. As a once-per-session ritual it lands;
        // on every sentence it becomes a 6-second tax the student pays over
        // and over, and the joke stops being a joke.
        if (msg.index === 1) playClassChatter();
        if (msg.sentence) App.state.sentences[msg.index] = msg.sentence;
        App.state.currentIndex = msg.index;
        App.saveSessionState();
        renderSentence(msg.index);
        highlightActive(msg.index);
        updateProgress(msg.index);
        document.getElementById('btn-mic').disabled = false;
        document.getElementById('btn-next').disabled = false;
        triggerGreeting();   // wave hello once on the first sentence
        setStatus('connected', 'Session ready. Click Start and read the sentence aloud.');
        break;

      /* The server's VAD heard the student start talking. If Vidya was
         mid-sentence, the server has already cancelled her turn — our job is
         just to go quiet immediately. Waiting for the next turn_start would
         leave her talking over the student for a full round trip. */
      case 'barge_in':
        stopPlayback();
        App.state.activeTurn = null;          // discard anything still in flight
        document.querySelectorAll('#ai-cloud .caret').forEach(c => c.remove());
        setTutorState('listening');
        setStatus('listening', 'Go ahead...');
        break;

      case 'speech_start':
        if (!micOn) break;
        /* BARGE-IN LIVES HERE, not on the server.
           TTS generates faster than realtime, so the server finishes a turn
           while the browser still has seconds of audio buffered. At that
           point the server sees no running turn and won't cancel anything —
           but Vidya is very much still talking. The browser is the only place
           that knows whether sound is actually coming out of the speaker. */
        if (isPlaying()) {
          stopPlayback();
          App.state.activeTurn = null;
          document.querySelectorAll('#ai-cloud .caret').forEach(c => c.remove());
          // Tell the server to abandon any generation still in flight.
          if (App.state.ws && App.state.ws.readyState === WebSocket.OPEN) {
            App.state.ws.send(JSON.stringify({ type: 'barge_in' }));
          }
        }
        setTutorState('listening');
        setStatus('listening', 'Listening...');
        break;

      case 'speech_end':
        if (!micOn) break;
        setTutorState('thinking');
        setStatus('processing', 'Vidya is thinking...');
        break;

      /* The student switched level by voice. The badge is the only place this
         is visible, so without this the UI silently disagrees with the lesson
         they're actually being given. */
      case 'level_changed':
        App.state.currentLevel = msg.level;
        App.state.currentIndex = 0;
        document.getElementById('active-level-badge').textContent =
          msg.level.charAt(0).toUpperCase() + msg.level.slice(1);

        /* Clearing the DOM is not enough. renderSentence() skips any index in
           App.state.renderedIndices, and the new level restarts at index 0 — which is
           already in there from the old level. Without this reset the list is
           wiped and then nothing draws, because every incoming index looks
           like one we've already shown. */
        App.state.sentences = {};          // matches its declaration: index → sentence
        App.state.renderedIndices = [];
        // Stale total would otherwise show "1 / 4" on a level with 6 App.state.sentences.
        App.state.totalSentences = msg.total || 0;
        document.getElementById('sentence-list').innerHTML = '';
        updateProgress(0);
        App.saveSessionState();
        setStatus('connected', `Switched to ${msg.level}.`);
        break;

      /* A safety response is about to play. Mute the mic for its duration.

         Two reasons, and the second is the one that actually bit: the alert
         beep plays out of the server machine's speakers, the mic hears it,
         VAD fires, and Vidya barge-ins herself out mid-sentence. And even
         without that, a safety message is the one reply in this app that
         should never be interrupted. */
      case 'safety_hold':
        suppressMic();
        break;

      case 'transcript': {
        const tel = document.getElementById('transcript-text');
        tel.textContent = msg.text;
        tel.classList.remove('empty');
        setTutorState('thinking');
        setStatus('processing', 'Vidya is thinking...');
        break;
      }

      /* A new utterance is starting. Anything still scheduled belongs to the
         previous turn and must go — this is also exactly what a barge-in
         will do, which is why turn_id exists before we need it. */
      case 'turn_start':
        stopPlayback();
        App.state.activeTurn = msg.turn_id;
        turnMarkStart = performance.now();
        gotFirstDelta = false;
        gotFirstAudio = false;
        document.getElementById('m-text').textContent = '—';
        document.getElementById('m-audio').textContent = '—';
        setCloudThinking();
        // Open the MediaSource now so the SourceBuffer is ready by the time
        // the first chunk lands — 'sourceopen' is async.
        startAudioStream();
        break;

      case 'ai_text_delta':
        if (msg.turn_id !== App.state.activeTurn) break;   // stale turn — discard
        if (!gotFirstDelta) { gotFirstDelta = true; mark('m-text'); }
        appendCloudDelta(msg.text);
        break;

      case 'ai_text':
        if (msg.turn_id && msg.turn_id !== App.state.activeTurn) break;
        // Only overwrite if we never streamed — otherwise the deltas already
        // built the same string and replacing it would flicker.
        if (!cloudBody) setCloudText(msg.text);
        break;

      case 'audio_chunk':
        if (msg.turn_id !== App.state.activeTurn) break;   // stale turn — discard
        scheduleChunk(msg.data);
        break;

      case 'audio_end':
        releaseMicSuppression();
        if (msg.turn_id !== App.state.activeTurn) break;
        // Remove the caret now that nothing more is coming.
        document.querySelectorAll('#ai-cloud .caret').forEach(c => c.remove());
        if (!gotFirstAudio) {
          // Nothing ever played — don't leave the UI stuck on "speaking".
          setTutorState('idle');
          setStatus('connected', 'Your turn — speak or ask a question.');
        } else {
          scheduleIdleAfterPlayback();
        }
        break;

      // Fallback path: backend couldn't open the TTS socket and sent one WAV.
      case 'ai_audio':
        releaseMicSuppression();
        await playBase64Wav(msg.data);
        break;

      case 'session_complete': {
        App.state.sessionOver = true;
        App.clearSessionState();
        if (micOn) stopMic();
        setTutorState('idle');
        document.getElementById('tutor-svg').classList.add('greeting');
        document.getElementById('btn-mic').disabled = true;

        const nxt = App.nextLevel(App.state.currentLevel);
        const nextBtn = document.getElementById('btn-next');
        if (nxt) {
          App.state.pendingNextLevel = nxt;
          nextBtn.disabled = false;
          nextBtn.textContent = `Start ${nxt.charAt(0).toUpperCase() + nxt.slice(1)} →`;
          document.getElementById('btn-mic').textContent = '✓ Level complete';
          setStatus('connected', `Level complete! Ready for ${nxt}? Tap Start ${nxt}.`);
        } else {
          App.state.pendingNextLevel = null;
          nextBtn.disabled = true;
          nextBtn.textContent = 'All done';
          document.getElementById('btn-mic').textContent = '✓ Course complete';
          setStatus('connected', '🎉 You finished all three levels. That is the whole course — shabaash!');
        }
        break;
      }

      case 'error':
        console.error('[WS error]', msg.message);
        stopPlayback();
        setTutorState('idle');
        setStatus('', `Error: ${msg.message}`);
        break;
    }
  };

  App.state.ws.onclose = (e) => {
    // The code is the only way to tell a deliberate close from a crash.
    // 1000/1001 = browser closed it on purpose (reload, navigation, tab
    // freeze). 1006 = died with no close frame, which points at a client-side
    // error rather than the server.
    console.warn('[WS] closed', { code: e.code, reason: e.reason, wasClean: e.wasClean });
    stopHeartbeat();
    stopPlayback();
    // The mic keeps capturing otherwise, sending into a dead socket.
    if (micOn) stopMic();
    scheduleReconnect();
  };
  App.state.ws.onerror = (e) => { console.error('[WS]', e); setStatus('', 'WebSocket error — check console.'); };
}



// ── Exports used cross-module via App.* ──────────────────────────────────
App.connectWS = connectWS;