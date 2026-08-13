/* tutor.js — the SVG tutor's state machine.
 *
 * Two exported functions:
 *   setTutorState(state)   — flips the top-level class on #tutor-svg. CSS in
 *                            tutor.css picks it up and runs the matching
 *                            animation set (idle float, listening tilt,
 *                            speaking nod, and the once-per-session greeting).
 *   triggerGreeting()      — fires the wave-hello animation exactly once on
 *                            the first sentence of a session.
 *
 * The tutor's appearance (clothing, hair) is owned by vidya-style.js. This
 * file is only about state and animation triggers.
 */

// ── Tutor character states ────────────────────────────────────────────────────
// 'idle' | 'listening' | 'thinking' | 'speaking'  (greeting is independent — the wave)
function setTutorState(state) {
  const svg = document.getElementById('tutor-svg');
  const wasGreeting = svg.classList.contains('greeting');
  svg.classList.remove('idle', 'listening', 'thinking', 'speaking');
  svg.classList.add(state);
  if (wasGreeting) svg.classList.add('greeting');  // don't cut a wave short
  const chip = document.getElementById('state-chip');
  chip.className = 'state-chip' + (state !== 'idle' ? ' ' + state : '');
  chip.textContent = { idle: 'Idle', listening: 'Listening', thinking: 'Thinking', speaking: 'Speaking' }[state];
}

let hasGreeted = false;
// Wave once on entry, then tuck the hand away.
function triggerGreeting() {
  if (hasGreeted) return;
  hasGreeted = true;
  const svg = document.getElementById('tutor-svg');
  svg.classList.add('greeting');
  // wave runs 2 × 0.75s = 1.5s, then fade the hand out
  setTimeout(() => svg.classList.remove('greeting'), 1600);
}