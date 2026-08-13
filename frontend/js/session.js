/* session.js — the lesson's own lifecycle, distinct from the WebSocket.
 *
 * Three responsibilities:
 *   1. POST /session/start to get a session_id
 *   2. Write the live position to sessionStorage as it changes
 *   3. On page load, if we find a fresh entry there, resume from it
 *
 * The last one is the one that matters. Reloading — for any reason, including
 * memory pressure or a crash — should drop the student back into their lesson,
 * not onto the landing screen. Reconnect logic can't help there, because the
 * script that would run it has been thrown away too.
 *
 * sessionStorage rather than localStorage on purpose: this is about one tab's
 * lesson in progress. It should not follow you into a new tab tomorrow.
 */

App.SESSION_KEY = 'sanskrit-coach:session';

App.saveSessionState = function () {
  try {
    if (!App.state.sessionId) return;
    sessionStorage.setItem(App.SESSION_KEY, JSON.stringify({
      sessionId: App.state.sessionId,
      level:     App.state.currentLevel,
      index:     App.state.currentIndex,
      total:     App.state.totalSentences,
      at:        Date.now(),
    }));
  } catch (e) {}
};

App.clearSessionState = function () {
  try { sessionStorage.removeItem(App.SESSION_KEY); } catch (e) {}
};

App.resumeSessionIfAny = function () {
  let s = null;
  try {
    const raw = sessionStorage.getItem(App.SESSION_KEY);
    s = raw ? JSON.parse(raw) : null;
  } catch (e) { return false; }

  // 30 minutes is the cutoff. Silently rejoining a lesson from yesterday
  // would be baffling — the student closed the tab hours ago and expects
  // a fresh start, not a mid-conversation Vidya.
  if (!s || !s.sessionId || Date.now() - (s.at || 0) > 30 * 60 * 1000) {
    App.clearSessionState();
    return false;
  }

  console.log(`[session] resuming ${s.sessionId} at sentence ${s.index}`);
  App.state.sessionId      = s.sessionId;
  App.state.currentLevel   = s.level || 'easy';
  App.state.currentIndex   = s.index || 0;
  App.state.totalSentences = s.total || 0;

  document.getElementById('landing').style.display = 'none';
  document.getElementById('active-level-badge').textContent =
    App.state.currentLevel.charAt(0).toUpperCase() + App.state.currentLevel.slice(1);
  App.setStatus('processing', 'Reconnecting to your session...');

  App.state.deliberateClose = false;
  App.connectWS(App.state.sessionId, App.state.currentLevel);
  return true;
};

// ── Session start ─────────────────────────────────────────────────────────────
App.startSession = async function (level) {
  App.state.deliberateClose  = false;
  App.state.sessionOver      = false;
  App.state.pendingNextLevel = null;
  App.state.currentIndex     = 0;

  // A new level replaces the last one. Everything the previous level rendered
  // has to go, otherwise the sentence list keeps its old items and the
  // Level → level_changed path doesn't handle that (a new session comes over
  // a new WebSocket, not as a level_changed message).
  App.state.sentences       = {};
  App.state.renderedIndices = [];
  App.state.totalSentences  = 0;
  document.getElementById('sentence-list').innerHTML = '';

  // Reset the two mode-y buttons back to their at-rest labels; session_complete
  // may have rewritten them.
  const nb = document.getElementById('btn-next');
  nb.textContent = 'Next →';
  nb.disabled = true;
  const mb = document.getElementById('btn-mic');
  mb.textContent = '🎙 Start talking';
  document.getElementById('tutor-svg').classList.remove('greeting');

  App.state.currentLevel = level;

  App.state.studentName = document.getElementById('student-name').value.trim().slice(0, 40);
  const picked = document.querySelector('input[name="gender"]:checked');
  App.state.studentGender = picked ? picked.value : 'neutral';
  App.saveProfile(App.state.studentName, App.state.studentGender);

  document.getElementById('landing').style.display = 'none';
  document.getElementById('active-level-badge').textContent =
    level.charAt(0).toUpperCase() + level.slice(1);
  App.setStatus('processing', 'Connecting...');

  // Chrome refuses to start an AudioContext without a user gesture, and this
  // click IS one. Warming it here means the first reply isn't silent.
  App.getPlayContext().resume().catch(() => {});

  try {
    const res = await fetch(`${App.API_BASE}/session/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level }),
    });
    const data = await res.json();
    App.state.sessionId      = data.session_id;
    App.state.totalSentences = data.total_sentences || 0;
    App.connectWS(App.state.sessionId, level);
  } catch (e) {
    App.setStatus('', 'Could not reach backend. Is the server running?');
  }
};