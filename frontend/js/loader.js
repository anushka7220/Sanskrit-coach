/* loader.js — hands off to the app when Render's cold-started backend is up.
 *
 * The bus animation is CSS. This file's whole job is to:
 *   1. Poll /health until the backend answers
 *   2. Wait for the bus to complete a lap (MIN_LAPS) so the animation isn't
 *      cut short mid-drive
 *   3. Fade the loader out and redirect to app.html
 *
 * Handoff is driven by the animation, not a timer, because Render's wake
 * time varies from milliseconds to nearly a minute — a fixed duration either
 * cuts the bus off or leaves the student watching an empty road.
 */


  /* Fills the cold-start wait, then hands off to the app. */
  const BACKEND = 'https://sanskrit-coach.onrender.com';
  const APP = 'app.html';

  /* Always show at least one complete trip.

     Waiting only on serverReady isn't enough: if Render is already awake
     (a recent visit, or a second load), /health answers in milliseconds and
     the very first animationiteration hands off — the student sees the bus
     for a fraction of a second, mid-entry, and it reads as a broken flash
     rather than an intro. Counting laps guarantees a full journey: entry,
     the pause where the kids are visible, and the exit. */
  const MIN_LAPS = 60;

  let serverReady = false;
  let handedOff = false;
  let lapsDone = 0;

  async function awake() {
    try {
      const r = await fetch(BACKEND + '/health', { cache: 'no-store' });
      return r.ok;
    } catch { return false; }
  }

  async function poll() {
    if (await awake()) { serverReady = true; return; }
    setTimeout(poll, 2000);
  }

  /* Driven by the animation, not a timer: wake time swings from milliseconds
     to nearly a minute, so any fixed duration either cuts the bus off
     mid-screen or leaves the student staring at an empty road. We go at the
     end of a lap, when the bus has just cleared the right edge. */
  const rig = document.querySelector('.bus-rig');
  rig.addEventListener('animationiteration', () => {
    lapsDone++;
    if (serverReady && lapsDone >= MIN_LAPS && !handedOff) handoff();
  });

  function handoff() {
    handedOff = true;
    document.body.style.transition = 'opacity 0.8s ease';
    document.body.style.opacity = '0';
    setTimeout(() => { window.location.href = APP; }, 820);
  }

  /* Reduced motion has no animation to count laps on, so fall back to a
     timer long enough to read the title. */
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const started = Date.now();
    const check = setInterval(() => {
      if (serverReady && Date.now() - started > 2500 && !handedOff) {
        clearInterval(check);
        handoff();
      }
    }, 400);
  }

  poll();