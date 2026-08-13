/* config.js — constants and the shared App namespace.
 *
 * WHY THIS EXISTS
 * ---------------
 * When the app was one file, every function shared one implicit scope. Split
 * across modules that all load as classic <script> tags, they still share one
 * scope (the global one), but relying on that is exactly what makes big JS
 * codebases fragile — anyone can shadow anyone.
 *
 * So we do the sharing on purpose: one namespace, App, that every module
 * reaches into. State that used to be a bare `let` at the top of the file
 * now lives at App.state.<name>. That gives us:
 *
 *   - a single place to see what state the app carries
 *   - no accidental collisions with browser globals or extensions
 *   - a search target: grep App.state.sessionId and you find every use
 *
 * IMPORTANT: this must load FIRST. Every other module reads from window.App.
 */

window.App = window.App || {};

// ── Endpoints ────────────────────────────────────────────────────────────────
// On localhost we talk to a local backend. In production RAILWAY_URL is filled
// in so the deployed frontend hits the deployed API. Left as one const so
// there's only one line to edit when the backend moves.
App.RAILWAY_URL = 'sanskrit-coach.onrender.com';
App.API_BASE = App.RAILWAY_URL ? `https://${App.RAILWAY_URL}` : 'http://localhost:8000';
App.WS_BASE  = App.RAILWAY_URL ? `wss://${App.RAILWAY_URL}`   : 'ws://localhost:8000';

// ── Audio rates ──────────────────────────────────────────────────────────────
// Different for capture and playback, because they're for different things.
// Sarvam's streaming endpoint delivers MP3 at 24 kHz; STT wants 16 kHz PCM.
// Trying to share one context downsamples TTS on the way out.
App.PLAYBACK_SAMPLE_RATE = 24000;
App.MIC_SAMPLE_RATE      = 16000;

// ── Level order ──────────────────────────────────────────────────────────────
// The Next button uses this to move to the next level after a session completes.
App.LEVEL_ORDER = ['easy', 'intermediate', 'hard'];
App.nextLevel = function (lvl) {
  const i = App.LEVEL_ORDER.indexOf(lvl);
  return (i >= 0 && i < App.LEVEL_ORDER.length - 1) ? App.LEVEL_ORDER[i + 1] : null;
};

// ── Shared runtime state ─────────────────────────────────────────────────────
// Everything mutable and cross-module lives here. If a value is only used
// inside one module, keep it local to that module — this object is for state
// several modules genuinely share.
App.state = {
  ws: null,
  sessionId: null,
  currentLevel: 'easy',
  sessionOver: false,
  pendingNextLevel: null,

  studentName: '',
  studentGender: 'neutral',

  sentences: {},         // index → sentence object
  totalSentences: 0,
  currentIndex: 0,
  renderedIndices: [],   // for tracking render order

  // Audio contexts, lazily created. Chrome blocks AudioContext creation
  // outside a user gesture, so we can't build them at load time.
  micContext: null,
  playContext: null,

  // Deliberate close of the WebSocket (goHome), so reconnect logic stays away.
  deliberateClose: false,

  // The turn we're currently rendering audio/text for; used by playback
  // and ws to discard messages from a cancelled turn.
  activeTurn: null,

  // Debug counter for MediaSource underruns; shown in the metrics row.
  underruns: 0,
};

// ── Navigation report (debug) ────────────────────────────────────────────────
// Distinguishes a fresh visit from a reload from back/forward. Reload
// specifically means the page restarted — reconnect logic can't help there
// because the script that would run it has been thrown away too.
(function reportNavigation() {
  try {
    const nav = performance.getEntriesByType('navigation')[0];
    const kind = nav ? nav.type : 'unknown';
    const n = (parseInt(sessionStorage.getItem('sc:loads') || '0', 10) || 0) + 1;
    sessionStorage.setItem('sc:loads', String(n));
    console.log(`[page] load #${n} — navigation type: ${kind}`);
    if (kind === 'reload' && n > 1) {
      console.warn('[page] this was a RELOAD. The socket did not drop — the ' +
                   'whole page restarted, which is why the landing screen came back.');
    }
  } catch (e) {}
})();