/* profile.js — the student's name and gender, remembered across sessions.
 *
 * Kept deliberately small. The rich stats (streak, time practised, level) live
 * in vidya-style.js because that's where the UI to view them lives too, and
 * splitting them between files was more confusing than it was worth.
 *
 * localStorage rather than sessionStorage: name and gender should persist
 * across tabs and browser restarts. sessionStorage is for the in-progress
 * lesson only (see session.js).
 */

App.PROFILE_KEY = 'sanskrit-coach:profile';

App.loadProfile = function () {
  try {
    const raw = localStorage.getItem(App.PROFILE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
};

App.saveProfile = function (name, gender) {
  try {
    // Merge into whatever's already there — the appearance menu also writes
    // to this same record and we don't want to clobber its fields.
    const cur = App.loadProfile() || {};
    localStorage.setItem(App.PROFILE_KEY, JSON.stringify({ ...cur, name, gender }));
  } catch (e) {
    console.warn('[profile] could not save', e);
  }
};

App.clearProfile = function () {
  try { localStorage.removeItem(App.PROFILE_KEY); } catch (e) {}
};

/* Pre-fill the landing form if we've met this student before. Called from
 * app.js on load, only when there's no live session to resume. */
App.applyStoredProfile = function () {
  const p = App.loadProfile();
  if (!p) return;
  const nameInput = document.getElementById('student-name');
  if (nameInput && p.name) nameInput.value = p.name;
  if (p.gender) {
    const radio = document.querySelector(`input[name="gender"][value="${p.gender}"]`);
    if (radio) radio.checked = true;
  }
};