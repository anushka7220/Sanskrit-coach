/* landing.js — the welcome screen and its animated demo panel.
 *
 * Two flows:
 *   1. Intro → level picker. forgetProfile / goToLevels / backToIntro handle
 *      the visibility toggling between them.
 *   2. Demo. A muted, cloned copy of the tutor SVG replays a scripted
 *      exchange so the student can see the app in motion before they commit
 *      to starting a session. cloneSvgWithUniqueIds is what prevents its
 *      gradient IDs from clashing with the real tutor's.
 *
 * initDemo() is called from app.js on load — this file just defines it.
 */

// ── Landing: steps + demo ────────────────────────────────────────────────────
/* The profile is asked once and remembered. Re-typing your name every time you
   open a tutor you already introduced yourself to is exactly the kind of small
   friction that makes software feel impersonal. */
const PROFILE_KEY = 'sanskrit-coach:profile';

App.loadProfile = function () {
  try {
    const raw = localStorage.getItem(PROFILE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    // Private browsing, disabled storage, or corrupt JSON. Never fatal —
    // the student just gets asked again.
    return null;
  }
}

App.saveProfile = function (name, gender) {
  try {
    localStorage.setItem(PROFILE_KEY, JSON.stringify({ name, gender }));
  } catch (e) {
    console.warn('[profile] could not save', e);
  }
}

function forgetProfile() {
  try { localStorage.removeItem(PROFILE_KEY); } catch (e) {}
  App.state.studentName = '';
  App.state.studentGender = 'neutral';
  document.getElementById('student-name').value = '';
  const neutral = document.querySelector('input[name="gender"][value="neutral"]');
  if (neutral) neutral.checked = true;
  backToIntro();
}


function goToLevels() {
  const intro = document.getElementById('step-intro');
  const level = document.getElementById('step-level');
  intro.classList.remove('is-in');   intro.classList.add('is-out-left');
  level.classList.remove('is-out-right'); level.classList.add('is-in');

  // Greet by name the moment we know it — the demo stops being generic and
  // starts being about this student.
  const typed = document.getElementById('student-name').value.trim();
  if (typed) startDemo(typed);
}

function backToIntro() {
  const intro = document.getElementById('step-intro');
  const level = document.getElementById('step-level');
  level.classList.remove('is-in'); level.classList.add('is-out-right');
  intro.classList.remove('is-out-left'); intro.classList.add('is-in');
}

/* A silent, looping preview of what a turn looks like. Deliberately not real
   audio: browsers block autoplay without a gesture, and a landing page that
   suddenly talks at you is worse than one that doesn't. */
let demoTimer = null;

function demoLines(name) {
  const who = name ? ` ${name}` : '';
  return [
    `नमस्ते${who}! आज हम 'रामः वनं गच्छति।' पढ़ेंगे।`,
    `इसका अर्थ है — राम वन जाते हैं।`,
    `अब आप इसे पढ़कर सुनाइए, मैं सुन रही हूँ।`,
    `बहुत बढ़िया! उच्चारण बिल्कुल सही था।`,
  ];
}

function startDemo(name) {
  clearTimeout(demoTimer);
  const el = document.getElementById('demo-text');
  const svg = document.getElementById('demo-figure').firstElementChild;
  const lines = demoLines(name);
  let li = 0;

  function typeLine() {
    const line = lines[li];
    let i = 0;
    if (svg) { svg.classList.remove('idle'); svg.classList.add('speaking'); }

    (function tick() {
      el.textContent = line.slice(0, i);
      if (i++ <= line.length) {
        demoTimer = setTimeout(tick, 38);
      } else {
        if (svg) { svg.classList.remove('speaking'); svg.classList.add('idle'); }
        li = (li + 1) % lines.length;
        demoTimer = setTimeout(typeLine, 1900);   // read pause between lines
      }
    })();
  }
  typeLine();
}

/* Clone an SVG, renaming every internal id and the references to them.

   A plain cloneNode() puts a second element with id="skin", id="hair" etc.
   into the document. SVG paint references are resolved by id, and with
   duplicates the browser picks one — here the clone's, because the landing
   sits earlier in the DOM. Once the landing is hidden, those gradients stop
   painting and the REAL tutor renders as an unfilled outline.

   So the clone gets its own id namespace and touches nothing the app uses. */
function cloneSvgWithUniqueIds(src, prefix) {
  const copy = src.cloneNode(true);

  const renames = [];
  copy.querySelectorAll('[id]').forEach(el => {
    const oldId = el.id;
    const newId = prefix + oldId;
    renames.push([oldId, newId]);
    el.id = newId;
  });
  if (!renames.length) return copy;

  // Attributes that can carry a url(#id) or #id reference.
  const REF_ATTRS = [
    'fill', 'stroke', 'filter', 'clip-path', 'mask', 'style',
    'href', 'xlink:href', 'marker-start', 'marker-mid', 'marker-end',
  ];

  copy.querySelectorAll('*').forEach(el => {
    REF_ATTRS.forEach(attr => {
      const val = el.getAttribute(attr);
      if (!val || val.indexOf('#') === -1) return;
      let out = val;
      renames.forEach(([oldId, newId]) => {
        out = out.split('url(#' + oldId + ')').join('url(#' + newId + ')');
        if (out === '#' + oldId) out = '#' + newId;
      });
      if (out !== val) el.setAttribute(attr, out);
    });
  });

  return copy;
}

function initDemo() {
  // Clone the app's tutor SVG instead of duplicating 150 lines of paths.
  const src = document.getElementById('tutor-svg');
  const host = document.getElementById('demo-figure');
  if (src && host && !host.firstElementChild) {
    const copy = cloneSvgWithUniqueIds(src, 'demo-');
    copy.id = 'demo-svg';
    copy.classList.add('idle');
    host.appendChild(copy);
  }
  startDemo('');
}