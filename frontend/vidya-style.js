/* ============================================================================
   vidya-style.js — the ⋯ menu: learning stats + Vidya's appearance
   ============================================================================

   Drop this in after the app's own script and it wires itself up. It owns two
   things that don't belong in the lesson code:

     1. The three learning numbers (streak / time practised / level), moved out
        of the header. They were competing with the lesson for attention every
        second the student was reading a sentence, which is the wrong trade —
        stats are something you check, not something you monitor.

     2. Vidya's clothing, colour, hairstyle and hair colour.

   HOW THE APPEARANCE CHANGES ARE APPLIED
   --------------------------------------
   The tutor is one inline SVG whose fills are gradient references — url(#kurta),
   url(#dupatta), url(#hair). So most of the work is just rewriting gradient
   stops: change two <stop> colours and every garment painted with that gradient
   updates at once, including the sleeve on the waving arm.

   Only the SHAPES need real swapping — a saree drape isn't a shirt collar. Those
   live in OUTFIT_OVERLAY / HAIRSTYLES below as small SVG fragments injected into
   dedicated <g> layers, so the base body and head are never rebuilt and none of
   the existing animation classes (head-group, eyelid, mouth) are disturbed.

   Everything persists in the same localStorage record as the student's name.
============================================================================ */

(function () {
  'use strict';

  const KEY = 'sanskrit-coach:profile';

  /* ── Palette ──────────────────────────────────────────────────────────────
     Drawn from the app's own swatch rather than a generic colour picker, so a
     customised Vidya still belongs on the page. Each entry is [light, dark] —
     the two stops of the gradient, which is what gives the cloth its fold. */
  const CLOTH_COLORS = {
    mauve:  ['#C398A2', '#B08590'],   // the default
    sage:   ['#B5B497', '#A5A487'],
    rose:   ['#E3B9A6', '#D5A491'],
    khaki:  ['#DFD8A8', '#D2CB95'],
    indigo: ['#7C79A8', '#5F5C8C'],
    teal:   ['#95B8B0', '#7CA096'],
  };

  const HAIR_COLORS = {
    ink:      ['#4A3550', '#37325A', '#241F3D'],   // the default
    black:    ['#3A3340', '#2A2530', '#1C1920'],
    brown:    ['#6B4A32', '#543824', '#3D281A'],
    auburn:   ['#8A4A38', '#6E3828', '#52281C'],
    grey:     ['#8E8A96', '#726E7C', '#565260'],
  };

  /* ── Outfits ──────────────────────────────────────────────────────────────
     Each is an overlay drawn ON TOP of the base torso. The base shape stays,
     so switching outfits can never break the silhouette or the shoulders. */
  const OUTFITS = {
    saree: {
      label: 'Saree',
      // Pallu draped over one shoulder and falling to the hem.
      overlay: `
        <path d="M58 258 C58 214 62 194 78 180 C89 170 99 166 110 164
                 C114 176 120 210 110 258 Z" fill="url(#dupatta)" opacity="0.92"/>
        <path d="M110 165 C120 200 122 230 120 258"
              stroke="rgba(55,50,90,0.14)" stroke-width="2" fill="none"/>
        <path d="M96 186 C104 200 106 226 100 252"
              stroke="rgba(255,255,255,0.22)" stroke-width="1.6" fill="none"/>`,
    },
    kurta: {
      label: 'Kurta',
      // Straight cut with a centre placket and a side slit.
      overlay: `
        <path d="M120 168 L120 258" stroke="rgba(55,50,90,0.16)" stroke-width="2"/>
        <circle cx="120" cy="196" r="2.2" fill="rgba(55,50,90,0.22)"/>
        <circle cx="120" cy="214" r="2.2" fill="rgba(55,50,90,0.22)"/>
        <path d="M72 232 L72 258" stroke="rgba(55,50,90,0.14)" stroke-width="2"/>
        <path d="M168 232 L168 258" stroke="rgba(55,50,90,0.14)" stroke-width="2"/>`,
    },
    shirt: {
      label: 'Shirt',
      // Collar, buttons, and a chest pocket.
      overlay: `
        <path d="M106 166 L120 182 L134 166 L128 162 L120 172 L112 162 Z"
              fill="rgba(255,255,255,0.85)"/>
        <path d="M120 182 L120 258" stroke="rgba(55,50,90,0.16)" stroke-width="1.8"/>
        <circle cx="120" cy="198" r="2" fill="rgba(255,255,255,0.7)"/>
        <circle cx="120" cy="216" r="2" fill="rgba(255,255,255,0.7)"/>
        <circle cx="120" cy="234" r="2" fill="rgba(255,255,255,0.7)"/>
        <rect x="138" y="196" width="18" height="20" rx="2"
              fill="none" stroke="rgba(55,50,90,0.16)" stroke-width="1.6"/>`,
    },
    top: {
      label: 'Top',
      // Simple round neck with a contrast hem band.
      overlay: `
        <path d="M104 166 Q120 184 136 166" fill="none"
              stroke="rgba(55,50,90,0.18)" stroke-width="2.4"/>
        <path d="M62 246 C62 246 90 252 120 252 C150 252 178 246 178 246
                 L180 258 L60 258 Z" fill="rgba(255,255,255,0.16)"/>`,
    },
  };

  /* ── Hairstyles ───────────────────────────────────────────────────────────
     Three, deliberately. Each replaces the back-hair and the accessory in one
     go; the bangs and face underneath are untouched. */
  const HAIRSTYLES = {
    bun: {
      label: 'Bun',
      svg: `
        <path d="M64 100 C58 48 92 26 120 26 C148 26 182 48 176 100
                 C176 128 168 144 158 152 L82 152 C72 144 64 128 64 100 Z" fill="url(#hair)"/>
        <circle cx="169" cy="46" r="15" fill="url(#hair)"/>
        <circle cx="173" cy="41" r="4.5" fill="#D2CB95"/>
        <circle cx="164" cy="49" r="3" fill="#D5A491"/>`,
    },

    /* A braid is read by its WEAVE, not by its outline. The first attempt was a
       smooth tapering tube with tick marks across it, which just looks like a
       rope. Real plaits are a stack of overlapping lobes that alternate sides,
       so that's what these are: six ellipses, rotated in opposite directions
       down the length and shrinking toward the tie. */
    braid: {
      label: 'Braid',
      svg: `
        <path d="M64 100 C58 48 92 26 120 26 C148 26 182 48 176 100
                 C176 128 168 144 158 152 L82 152 C72 144 64 128 64 100 Z" fill="url(#hair)"/>
        <!-- hair gathered and swept to one side before the plait begins -->
        <path d="M150 108 C168 112 178 122 176 138 C170 130 160 124 148 122 Z" fill="url(#hair)"/>
        <g fill="url(#hair)">
          <ellipse cx="170" cy="132" rx="13"   ry="11"  transform="rotate(22 170 132)"/>
          <ellipse cx="177" cy="150" rx="12.5" ry="10.5" transform="rotate(-22 177 150)"/>
          <ellipse cx="172" cy="168" rx="12"   ry="10"  transform="rotate(22 172 168)"/>
          <ellipse cx="178" cy="185" rx="11"   ry="9.5" transform="rotate(-22 178 185)"/>
          <ellipse cx="174" cy="200" rx="9.8"  ry="8.8" transform="rotate(22 174 200)"/>
          <ellipse cx="179" cy="213" rx="8.4"  ry="7.8" transform="rotate(-22 179 213)"/>
        </g>
        <!-- the seams where the strands cross; this is what sells it as woven -->
        <g stroke="rgba(0,0,0,0.20)" stroke-width="1.4" fill="none" stroke-linecap="round">
          <path d="M162 140 q10 4 20 1"/>
          <path d="M166 158 q10 4 19 1"/>
          <path d="M164 176 q10 4 19 1"/>
          <path d="M168 193 q9 3 17 1"/>
          <path d="M167 207 q8 3 15 1"/>
        </g>
        <g stroke="rgba(255,255,255,0.14)" stroke-width="1.4" fill="none" stroke-linecap="round">
          <path d="M165 131 q9 -3 16 0"/>
          <path d="M169 167 q9 -3 15 0"/>
        </g>
        <!-- tie and tail -->
        <path d="M172 219 h13 v5 h-13 z" fill="#D5A491"/>
        <path d="M176 224 q-3 9 -7 13 M180 224 q1 9 4 12"
              stroke="url(#hair)" stroke-width="3" fill="none" stroke-linecap="round"/>`,
    },

    /* Open hair has to be a single MASS with weight, not two side strands. The
       earlier version drew a thin tube on each side, which reads as pigtails.
       This is one silhouette that sits behind the face — the face ellipse is
       painted after it, so the middle is covered and only the fall shows. */
    open: {
      label: 'Open',
      svg: `
        <path d="M120 20
                 C80 20 50 48 54 102
                 C44 146 42 194 50 232
                 C55 244 78 245 83 230
                 C77 194 79 152 88 128
                 C97 112 110 105 120 105
                 C130 105 143 112 152 128
                 C161 152 163 194 157 230
                 C162 245 185 244 190 232
                 C198 194 196 146 186 102
                 C190 48 160 20 120 20 Z" fill="url(#hair)"/>
        <!-- a couple of strands so the mass has direction instead of reading flat -->
        <g stroke="rgba(255,255,255,0.13)" stroke-width="2.6" fill="none" stroke-linecap="round">
          <path d="M62 120 q-6 52 -2 96"/>
          <path d="M74 132 q-5 44 -2 82"/>
          <path d="M178 120 q6 52 2 96"/>
          <path d="M166 132 q5 44 2 82"/>
        </g>
        <!-- soft parting at the crown -->
        <path d="M120 24 q-3 16 -2 28" stroke="rgba(255,255,255,0.10)"
              stroke-width="2.2" fill="none" stroke-linecap="round"/>`,
    },
  };

  const DEFAULTS = {
    outfit: 'saree', cloth: 'mauve', hairstyle: 'bun', hairColor: 'ink',
  };

  // ── Storage ──────────────────────────────────────────────────────────────
  function read() {
    try { return JSON.parse(localStorage.getItem(KEY)) || {}; }
    catch (e) { return {}; }
  }
  function write(patch) {
    try { localStorage.setItem(KEY, JSON.stringify({ ...read(), ...patch })); }
    catch (e) { console.warn('[style] could not save', e); }
  }
  function look() {
    const p = read();
    return {
      outfit:    OUTFITS[p.outfit]        ? p.outfit    : DEFAULTS.outfit,
      cloth:     CLOTH_COLORS[p.cloth]    ? p.cloth     : DEFAULTS.cloth,
      hairstyle: HAIRSTYLES[p.hairstyle]  ? p.hairstyle : DEFAULTS.hairstyle,
      hairColor: HAIR_COLORS[p.hairColor] ? p.hairColor : DEFAULTS.hairColor,
    };
  }

  // ── Applying the look ────────────────────────────────────────────────────
  function setStops(gradientId, colors) {
    const g = document.getElementById(gradientId);
    if (!g) return;
    const stops = g.querySelectorAll('stop');
    colors.forEach((c, i) => { if (stops[i]) stops[i].setAttribute('stop-color', c); });
  }

  /* Layers are created once and reused. Building them lazily means this file
     works whether it loads before or after the SVG is in the DOM. */
  function layer(id, insertBefore) {
    let el = document.getElementById(id);
    if (el) return el;
    const svg = document.getElementById('tutor-svg');
    if (!svg) return null;
    el = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    el.id = id;
    if (insertBefore && insertBefore.parentNode) {
      insertBefore.parentNode.insertBefore(el, insertBefore);
    } else {
      svg.appendChild(el);
    }
    return el;
  }

  function applyLook() {
    const L = look();
    const svg = document.getElementById('tutor-svg');
    if (!svg) return;

    // Colours: one gradient edit repaints every garment using it.
    setStops('kurta', CLOTH_COLORS[L.cloth]);
    setStops('hair',  HAIR_COLORS[L.hairColor]);

    // The saree's pallu reads best in a second, quieter colour. Other outfits
    // don't use the dupatta gradient at all, so this is harmless for them.
    const c = CLOTH_COLORS[L.cloth];
    setStops('dupatta', L.outfit === 'saree'
      ? CLOTH_COLORS.sage
      : [c[1], c[1]]);

    // Outfit overlay sits above the torso but below the head group, so a
    // collar can never cover the chin.
    const head = svg.querySelector('.head-group');
    const outfitLayer = layer('vidya-outfit', head);
    if (outfitLayer) outfitLayer.innerHTML = OUTFITS[L.outfit].overlay;

    // Hair replaces the original back-hair, which we hide rather than delete
    // so the app's own markup stays intact.
    const original = svg.querySelector('.head-group > path[fill="url(#hair)"]');
    if (original) original.style.display = 'none';
    svg.querySelectorAll('.head-group > circle[fill="url(#hair)"]')
       .forEach(el => { el.style.display = 'none'; });

    const headG = svg.querySelector('.head-group');
    if (headG) {
      let hairLayer = document.getElementById('vidya-hair');
      if (!hairLayer) {
        hairLayer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        hairLayer.id = 'vidya-hair';
        headG.insertBefore(hairLayer, headG.firstChild);
      }
      hairLayer.innerHTML = HAIRSTYLES[L.hairstyle].svg;
    }
  }

  // ── Stats ────────────────────────────────────────────────────────────────
  function formatTime(sec) {
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m`;
    return `${Math.floor(sec || 0)}s`;
  }

  function renderStats() {
    const p = read();
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    const lvl = p.level || 'easy';
    set('m-streak', p.streak || 0);
    set('m-time',   formatTime(p.seconds || 0));
    set('m-level',  lvl.charAt(0).toUpperCase() + lvl.slice(1));
  }

  // ── Menu UI ──────────────────────────────────────────────────────────────
  function styles() {
    const css = `
      .kebab {
        width: 34px; height: 34px; border-radius: 50%; border: 1.5px solid var(--line);
        background: transparent; cursor: pointer; display: inline-flex;
        align-items: center; justify-content: center; gap: 3px; padding: 0;
        transition: border-color .18s, background .18s;
      }
      .kebab:hover { border-color: var(--mauve); background: var(--ivory); }
      .kebab i { width: 3.5px; height: 3.5px; border-radius: 50%; background: var(--ink-soft); }
      .kebab[aria-expanded="true"] { border-color: var(--mauve); background: var(--ivory); }

      .menu {
        position: absolute; top: 58px; right: 24px; width: 296px; z-index: 60;
        background: var(--ivory); border: 1px solid var(--line); border-radius: 16px;
        box-shadow: 0 16px 40px rgba(55,50,90,0.16);
        padding: 18px; display: none;
      }
      .menu.open { display: block; }
      .menu h4 {
        font-size: 10px; letter-spacing: .16em; text-transform: uppercase;
        color: var(--ink-soft); font-weight: 700; margin: 0 0 10px;
      }
      .menu hr { border: 0; border-top: 1px solid var(--line); margin: 18px 0; }

      .m-stats { display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; }
      .m-stat { text-align: center; padding: 10px 4px; border-radius: 11px; background: var(--cream); }
      .m-stat b {
        display: block; font-family: 'Fraunces', serif; font-size: 19px;
        color: var(--ink); font-variant-numeric: tabular-nums; line-height: 1.1;
      }
      .m-stat.streak b { color: #C0603F; }
      .m-stat span {
        display: block; font-size: 9px; letter-spacing: .09em; text-transform: uppercase;
        color: var(--ink-soft); margin-top: 4px;
      }

      .m-row { margin-bottom: 14px; }
      .m-row:last-child { margin-bottom: 0; }
      .m-label { font-size: 11px; color: var(--ink-soft); margin-bottom: 7px; }
      .chips { display: flex; flex-wrap: wrap; gap: 6px; }
      .chip {
        font: inherit; font-size: 12px; padding: 6px 12px; border-radius: 999px;
        border: 1px solid var(--line); background: transparent; color: var(--ink);
        cursor: pointer; transition: all .16s;
      }
      .chip:hover { border-color: var(--mauve); }
      .chip[aria-pressed="true"] {
        background: var(--ink); border-color: var(--ink); color: var(--ivory);
      }
      .swatches { display: flex; flex-wrap: wrap; gap: 8px; }
      .sw {
        width: 26px; height: 26px; border-radius: 50%; cursor: pointer; padding: 0;
        border: 2px solid transparent; box-shadow: 0 0 0 1px var(--line) inset;
        transition: transform .16s, border-color .16s;
      }
      .sw:hover { transform: scale(1.12); }
      .sw[aria-pressed="true"] { border-color: var(--ink); transform: scale(1.12); }

      .m-reset {
        margin-top: 16px; width: 100%; font: inherit; font-size: 12px;
        padding: 9px; border-radius: 10px; border: 1px solid var(--line);
        background: transparent; color: var(--ink-soft); cursor: pointer;
      }
      .m-reset:hover { border-color: var(--mauve); color: var(--ink); }
      @media (max-width: 520px) { .menu { right: 12px; width: calc(100vw - 24px); } }
    `;
    const tag = document.createElement('style');
    tag.textContent = css;
    document.head.appendChild(tag);
  }

  function chipRow(label, options, current, onPick) {
    const row = document.createElement('div');
    row.className = 'm-row';
    row.innerHTML = `<div class="m-label">${label}</div><div class="chips"></div>`;
    const box = row.querySelector('.chips');
    Object.entries(options).forEach(([key, val]) => {
      const b = document.createElement('button');
      b.className = 'chip';
      b.type = 'button';
      b.textContent = val.label;
      b.setAttribute('aria-pressed', key === current);
      b.onclick = () => onPick(key);
      box.appendChild(b);
    });
    return row;
  }

  function swatchRow(label, colors, current, onPick) {
    const row = document.createElement('div');
    row.className = 'm-row';
    row.innerHTML = `<div class="m-label">${label}</div><div class="swatches"></div>`;
    const box = row.querySelector('.swatches');
    Object.entries(colors).forEach(([key, c]) => {
      const b = document.createElement('button');
      b.className = 'sw';
      b.type = 'button';
      b.title = key;
      b.setAttribute('aria-label', key);
      b.style.background = `linear-gradient(160deg, ${c[0]}, ${c[c.length - 1]})`;
      b.setAttribute('aria-pressed', key === current);
      b.onclick = () => onPick(key);
      box.appendChild(b);
    });
    return row;
  }

  function buildMenu() {
    const menu = document.getElementById('vidya-menu');
    const L = look();
    menu.innerHTML = `
      <h4>Your learning</h4>
      <div class="m-stats">
        <div class="m-stat streak"><b id="m-streak">0</b><span>day streak</span></div>
        <div class="m-stat"><b id="m-time">0s</b><span>practised</span></div>
        <div class="m-stat"><b id="m-level">Easy</b><span>level</span></div>
      </div>
      <hr>
      <h4>Vidya's look</h4>
      <div id="m-controls"></div>
      <button class="m-reset" type="button" id="m-reset">Reset to default</button>
    `;

    const c = menu.querySelector('#m-controls');
    const pick = patch => { write(patch); applyLook(); buildMenu(); };

    c.appendChild(chipRow('Clothing', OUTFITS,    L.outfit,    v => pick({ outfit: v })));
    c.appendChild(swatchRow('Colour', CLOTH_COLORS, L.cloth,   v => pick({ cloth: v })));
    c.appendChild(chipRow('Hairstyle', HAIRSTYLES, L.hairstyle, v => pick({ hairstyle: v })));
    c.appendChild(swatchRow('Hair colour', HAIR_COLORS, L.hairColor, v => pick({ hairColor: v })));

    menu.querySelector('#m-reset').onclick = () => { write(DEFAULTS); applyLook(); buildMenu(); };

    renderStats();
  }

  function mount() {
    const header = document.querySelector('header .level-badges');
    if (!header || document.getElementById('vidya-kebab')) return;

    styles();

    const btn = document.createElement('button');
    btn.id = 'vidya-kebab';
    btn.className = 'kebab';
    btn.type = 'button';
    btn.title = 'Stats and appearance';
    btn.setAttribute('aria-label', 'Stats and appearance');
    btn.setAttribute('aria-expanded', 'false');
    btn.style.flexDirection = 'column';
    btn.innerHTML = '<i></i><i></i><i></i>';
    header.appendChild(btn);

    const menu = document.createElement('div');
    menu.id = 'vidya-menu';
    menu.className = 'menu';
    document.body.appendChild(menu);

    btn.onclick = e => {
      e.stopPropagation();
      const open = menu.classList.toggle('open');
      btn.setAttribute('aria-expanded', open);
      if (open) buildMenu();
    };
    // Clicking the menu itself must not close it.
    menu.addEventListener('click', e => e.stopPropagation());
    document.addEventListener('click', () => {
      menu.classList.remove('open');
      btn.setAttribute('aria-expanded', 'false');
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        menu.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
      }
    });

    applyLook();
  }

  // Stats keep ticking while the menu is shut; refresh them when it's open.
  setInterval(() => {
    const m = document.getElementById('vidya-menu');
    if (m && m.classList.contains('open')) renderStats();
  }, 5000);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }

  // Exposed so the app can re-apply after it clones or rebuilds the tutor SVG.
  window.VidyaStyle = { apply: applyLook, refresh: renderStats };
})();