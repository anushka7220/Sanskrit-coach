/* onclick attributes in app.html call these by their bare names
 * (startSession, goHome, toggleMic, moveOn, goToLevels, backToIntro,
 *  forgetProfile). After the split those live under App.*, so we alias them
 * onto window here to avoid rewriting every onclick.
 *
 * Small also-owned helpers like showFallbackPlayback stay local to their
 * module; anything reachable from an onclick has to be aliased below.
 */
[
  'startSession', 'goHome', 'goToLevels', 'backToIntro',
  'forgetProfile', 'clearProfile',
].forEach(name => {
  if (typeof App[name] === 'function') window[name] = App[name];
});

/* app.js — the entry point that wires everything together.
 *
 * Every other module defines things; this one runs them. Kept intentionally
 * small so the wiring is one place, easy to follow:
 *
 *   1. initDemo()              — build the landing demo now that its target
 *                                DOM exists.
 *   2. resumeSessionIfAny()    — if there's a live lesson to resume, jump
 *                                straight into it and skip the landing.
 *   3. applyStoredProfile()    — otherwise, pre-fill the name gate from the
 *                                remembered profile so returning students
 *                                don't have to type their name every time.
 *
 * Loaded LAST (after everything it references). Order in app.html matters.
 */

// Build the landing demo once the DOM (and the tutor SVG it clones) exists.
initDemo();
// A lesson in progress takes priority over the landing screen — otherwise a
// reload throws the student back to picking a level they already picked.
if (!App.resumeSessionIfAny()) {
  App.applyStoredProfile();
}