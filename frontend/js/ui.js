/* ui.js — everything the student sees between the tutor and the buttons.
 *
 * Three groups of functions:
 *
 *   Cloud  — setCloudThinking, appendCloudDelta, setCloudText.
 *            The speech bubble above the tutor. Streaming replies flow into
 *            it token by token; the trailing caret is the visual cue that
 *            more text is on its way.
 *
 *   Karaoke — renderSentence, highlightActive, updateProgress.
 *            The scrollable list on the left showing where the student is in
 *            the lesson. Sentences render lazily as `next_sentence` arrives,
 *            so a resumed session doesn't paint blank slots.
 *
 *   Chrome — setStatus, goHome.
 *            The status line at the bottom of the tutor panel, and the full
 *            teardown when the student chooses to leave. goHome is the ONE
 *            place App.state.deliberateClose is set, which is what tells the
 *            reconnect logic in ws.js to stay away.
 */

// State this file owns:
let cloudBody = null;      // the streaming <span> inside the cloud

// ── Speech cloud (the streaming reply above the tutor) ──────────────────
// ── Cloud (streaming text) ────────────────────────────────────────────────────

function setCloudThinking() {
  const cloud = document.getElementById('ai-cloud');
  cloud.classList.remove('empty', 'speaking');
  cloud.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';
  cloudBody = null;
}

/* Append a streamed fragment. The first delta clears the thinking dots and
   installs a text node + caret; subsequent deltas just extend the node, so
   the browser isn't re-parsing the whole reply on every token. */
function appendCloudDelta(text) {
  const cloud = document.getElementById('ai-cloud');
  if (!cloudBody) {
    cloud.classList.remove('empty');
    cloud.classList.add('speaking');
    cloud.innerHTML = '';
    cloudBody = document.createTextNode('');
    cloud.appendChild(cloudBody);
    const caret = document.createElement('span');
    caret.className = 'caret';
    cloud.appendChild(caret);
  }
  cloudBody.appendData(text);
}

/* Final authoritative text — also the path used when the backend falls back
   to one-shot REST synthesis and never sends deltas. */
function setCloudText(text) {
  const cloud = document.getElementById('ai-cloud');
  cloud.classList.remove('empty');
  cloud.classList.add('speaking');
  cloud.textContent = text;
  cloudBody = null;
}

/* Is this a fresh visit or a reload?

   A reload and a dropped socket look identical from the server: both show up
   as code 1001 and then silence. But they need completely different fixes —
   reconnect logic can't help a page that no longer exists, because the script
   that would run it has been thrown away too.

   This makes the difference visible instead of guessable. */



// ── Karaoke sentence rendering ──────────────────────────────────────────
// ── Karaoke rendering ─────────────────────────────────────────────────────────
function renderSentence(index) {
  if (App.state.renderedIndices.includes(index)) return;

  const s = App.state.sentences[index];
  if (!s) return;

  const list = document.getElementById('sentence-list');
  if (App.state.renderedIndices.length === 0) list.innerHTML = '';

  const item = document.createElement('div');
  item.className = 'sentence-item';
  item.id = `sentence-${index}`;
  item.innerHTML = `
    <div class="sentence-num"><span>${index + 1}</span></div>
    <div class="skt-text">${s.sanskrit}</div>
    <div class="translit">${s.transliteration}</div>
    <div class="sentence-meta">${s.meaning_en}</div>
  `;
  list.appendChild(item);
  App.state.renderedIndices.push(index);
}

function highlightActive(activeIndex) {
  App.state.renderedIndices.forEach(i => {
    const el = document.getElementById(`sentence-${i}`);
    if (!el) return;
    el.classList.remove('active', 'done');
    if (i === activeIndex) el.classList.add('active');
    else if (i < activeIndex) el.classList.add('done');
  });

  const active = document.getElementById(`sentence-${activeIndex}`);
  if (active) setTimeout(() => active.scrollIntoView({ behavior: 'smooth', block: 'center' }), 100);
}

/* total comes from /session/start now — the old version counted App.state.sentences
   already received, so the denominator grew as you progressed. */
function updateProgress(index) {
  const total = App.state.totalSentences || Object.keys(App.state.sentences).length || 1;
  document.getElementById('progress-fill').style.width =
    `${Math.round((index / total) * 100)}%`;
  document.getElementById('progress-count').textContent = `${index + 1} / ${total}`;
}


// ── Status line and 'go home' teardown ──────────────────────────────────
// ── UI helpers ────────────────────────────────────────────────────────────────
function setStatus(type, text) {
  const dot = document.getElementById('status-dot');
  dot.className = 'status-dot' + (type ? ' ' + type : '');
  document.getElementById('status-text').textContent = text;
}

function goHome() {
  // The one genuinely deliberate close: the student chose to leave the
  // session. Everything else — freezes, discards, network blips — should
  // reconnect, which is why this flag is set HERE and nowhere else.
  App.state.deliberateClose = true;
  App.clearSessionState();
  stopHeartbeat();
  stopPlayback();
  if (App.state.ws) App.state.ws.close();
  App.state.ws = null; App.state.sessionId = null; App.state.sentences = {}; App.state.currentIndex = 0; App.state.renderedIndices = [];
  App.state.totalSentences = 0; App.state.activeTurn = null; App.state.underruns = 0;
  document.getElementById('m-text').textContent = '—';
  document.getElementById('m-audio').textContent = '—';
  document.getElementById('m-underrun').textContent = '0';
  document.getElementById('sentence-list').innerHTML =
    '<div class="placeholder">Choose a level to begin.</div>';
  const tel = document.getElementById('transcript-text');
  tel.textContent = 'Start speaking — your words will appear here.';
  tel.className = 'heard-text empty';
  const cloud = document.getElementById('ai-cloud');
  cloud.textContent = "The tutor's reply appears here — namaste, ready when you are.";
  cloud.className = 'cloud empty';
  cloudBody = null;
  document.getElementById('progress-fill').style.width = '0%';
  document.getElementById('progress-count').textContent = '0 / 0';
  document.getElementById('btn-mic').disabled = true;
  document.getElementById('btn-next').disabled = true;
  setTutorState('idle');
  document.getElementById('tutor-svg').classList.add('greeting');
  setStatus('', 'Not connected. Choose a level to begin.');
  // grid, not flex — the landing is a two-column layout now.
  document.getElementById('landing').style.display = 'grid';
  backToIntro();
  startDemo('');
}



// ── Exports used cross-module via App.* ──────────────────────────────────
App.setStatus = setStatus;