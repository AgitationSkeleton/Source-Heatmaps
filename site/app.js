// Source Heatmaps viewer. Static, read-only: fetches same-origin files under data/ and icons/ only.
import * as THREE from './vendor/three.module.min.js';
import { OrbitControls } from './vendor/OrbitControls.js';

const $ = (id) => document.getElementById(id);
const GAME_RE = /^[a-z0-9_]{1,32}$/;
const MAP_RE = /^[a-z0-9_.+\-]{1,96}$/;
const COLOR_RE = /^#[0-9a-f]{6}$/i;
const ICON_RE = /^[a-z0-9_]+(\/[a-z0-9_]+)?\.png$/;

const KIND_GROUND = 0, KIND_DEATH = 1, KIND_AIR = 3;
const MOBILE = matchMedia('(max-width: 760px)').matches || matchMedia('(pointer: coarse)').matches;

// ---------------------------------------------------------------- state
const S = {
  manifest: null, game: null, map: null, meta: null,
  cells: null, edges: null, nav: null, analysis: null,
  layer: 'presence', colorMode: 'heat', incAir: false,
  humans: true, bots: true,
  teamsOff: new Set(), classesOff: new Set(), linesOn: new Set(),
  lineMin: 3, cut: null, psize: 1, overWalls: true,
  loadToken: 0, navSrc: null, navSources: [], wp: null, curAnalysis: null,
};

// ---------------------------------------------------------------- three.js scene
const canvas = $('gl');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: !MOBILE, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MOBILE ? 1.5 : 2));
renderer.localClippingEnabled = true;
const scene = new THREE.Scene();
scene.background = new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() || '#15181d');
const camera = new THREE.PerspectiveCamera(45, 1, 8, 200000);
camera.up.set(0, 0, 1);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.screenSpacePanning = true;
controls.touches = { ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN };
scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.4);
sun.position.set(0.4, 0.25, 1);
scene.add(sun);

const clip = new THREE.Plane(new THREE.Vector3(0, 0, -1), 1e9);
const clipList = [clip];
const groups = { geo: new THREE.Group(), heat: new THREE.Group(), lines: new THREE.Group(), nav: new THREE.Group(), navLinks: new THREE.Group(), gaps: new THREE.Group(), missing: new THREE.Group() };
Object.values(groups).forEach((g) => scene.add(g));

function disposeGroup(g) {
  for (const o of [...g.children]) {
    g.remove(o);
    o.geometry?.dispose();
    if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => { m.map?.dispose(); m.dispose(); });
  }
}

function resize() {
  const r = canvas.parentElement.getBoundingClientRect();
  renderer.setSize(r.width, r.height, false);
  camera.aspect = r.width / Math.max(1, r.height);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(canvas.parentElement);

let needsRender = true;
controls.addEventListener('change', () => { needsRender = true; });
function loop() {
  requestAnimationFrame(loop);
  if (controls.update() || needsRender) {
    renderer.render(scene, camera);
    needsRender = false;
  }
}
loop();
const redraw = () => { needsRender = true; };

// PointsMaterial "size" with attenuation is size*(h/2)/depth pixels; dividing a world size by
// tan(fov/2) makes a point cover that many world units on screen
const worldPt = (units) => units / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));

// round sprite for points
const dot = (() => {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, 'rgba(255,255,255,1)');
  grd.addColorStop(0.55, 'rgba(255,255,255,0.9)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd;
  g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
})();

// inferno-like ramp
const RAMP = [[0x1b, 0x0c, 0x41], [0x6a, 0x17, 0x6e], [0xbb, 0x37, 0x54], [0xf9, 0x8e, 0x09], [0xfc, 0xff, 0xa4]];
function ramp(t) {
  t = Math.min(1, Math.max(0, t)) * (RAMP.length - 1);
  const i = Math.min(RAMP.length - 2, Math.floor(t)), f = t - i;
  const a = RAMP[i], b = RAMP[i + 1];
  return [(a[0] + (b[0] - a[0]) * f) / 255, (a[1] + (b[1] - a[1]) * f) / 255, (a[2] + (b[2] - a[2]) * f) / 255];
}
function hexRgb(h) {
  const v = parseInt(COLOR_RE.test(h) ? h.slice(1) : '9aa0a6', 16);
  return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255];
}

// ---------------------------------------------------------------- data loading
async function getJSON(path) {
  const r = await fetch(path, { credentials: 'omit', cache: 'no-cache' });
  if (!r.ok) throw new Error(r.status + ' ' + path);
  return r.json();
}
async function getBin(path) {
  const r = await fetch(path, { credentials: 'omit', cache: 'no-cache' });
  if (!r.ok) throw new Error(r.status + ' ' + path);
  return r.arrayBuffer();
}
function magic(buf, m) {
  const u = new Uint8Array(buf, 0, 4);
  return String.fromCharCode(...u) === m;
}
function parseCells(buf) {
  if (!magic(buf, 'HMC1')) throw new Error('bad cells file');
  const n = new DataView(buf).getUint32(4, true);
  let o = 8;
  const take = (T, bytes) => { const a = new T(buf.slice(o, o + n * bytes)); o += n * bytes; return a; };
  return { len: n, n: take(Uint32Array, 4), x: take(Int16Array, 2), y: take(Int16Array, 2), z: take(Int16Array, 2),
    kind: take(Uint8Array, 1), team: take(Uint8Array, 1), cls: take(Uint8Array, 1), bot: take(Uint8Array, 1) };
}
function parseEdges(buf) {
  if (!magic(buf, 'HME1')) throw new Error('bad edges file');
  const n = new DataView(buf).getUint32(4, true);
  let o = 8;
  const take = (T, bytes) => { const a = new T(buf.slice(o, o + n * bytes)); o += n * bytes; return a; };
  const e = { len: n, n: take(Uint32Array, 4) };
  for (const k of ['x', 'y', 'z', 'x2', 'y2', 'z2']) e[k] = take(Int16Array, 2);
  for (const k of ['team', 'cls', 'bot']) e[k] = take(Uint8Array, 1);
  return e;
}
function parseGeo(buf) {
  if (!magic(buf, 'HMG1')) throw new Error('bad geometry file');
  const dv = new DataView(buf);
  const nv = dv.getUint32(4, true), ni = dv.getUint32(8, true), isz = dv.getUint32(12, true);
  let o = 16;
  const pos = new Int16Array(buf.slice(o, o + nv * 6));
  o += nv * 6;
  if (o % 4) o += 4 - (o % 4);
  const idx = isz === 2 ? new Uint16Array(buf.slice(o, o + ni * 2)) : new Uint32Array(buf.slice(o, o + ni * 4));
  return { pos: new Float32Array(pos), idx };
}

function base() { return `data/${S.game}/${S.map}/`; }

async function loadManifest() {
  status('Loading…');
  S.manifest = await getJSON('data/manifest.json');
  const sel = $('gameSel');
  sel.replaceChildren();
  const games = Object.keys(S.manifest.games || {}).filter((g) => GAME_RE.test(g));
  games.sort((a, b) => S.manifest.games[a].name.localeCompare(S.manifest.games[b].name));
  for (const g of games) sel.append(new Option(S.manifest.games[g].name, g));
  if (!games.length) { status('No data yet.'); return; }
  const [hg, hm] = parseHash();
  S.game = games.includes(hg) ? hg : games[0];
  sel.value = S.game;
  fillMaps(hm);
}

function fillMaps(prefer) {
  const g = S.manifest.games[S.game];
  const maps = (g.maps || []).filter((m) => MAP_RE.test(m.map));
  maps.sort((a, b) => b.samples - a.samples);
  const sel = $('mapSel');
  sel.replaceChildren();
  for (const m of maps) {
    const hrs = (m.samples / 3600).toFixed(m.samples < 36000 ? 1 : 0);
    sel.append(new Option(`${m.map}  (${hrs} h${m.geo ? '' : ', no geometry'})`, m.map));
  }
  S.map = maps.some((m) => m.map === prefer) ? prefer : (maps[0] && maps[0].map);
  if (S.map) { sel.value = S.map; loadMap(); }
}

function parseHash() {
  let h;
  try { h = decodeURIComponent(location.hash.replace(/^#/, '')).split('/'); } catch { h = []; }
  return [GAME_RE.test(h[0] || '') ? h[0] : null, MAP_RE.test(h[1] || '') ? h[1] : null];
}

async function loadMap() {
  if (!GAME_RE.test(S.game) || !MAP_RE.test(S.map)) return;
  const token = ++S.loadToken;
  history.replaceState(null, '', `#${S.game}/${S.map}`);
  status(`Loading ${S.map}…`);
  ['geo', 'heat', 'lines', 'nav', 'navLinks', 'gaps', 'missing'].forEach((k) => disposeGroup(groups[k]));
  S.cells = S.edges = S.nav = S.analysis = S.wp = S.curAnalysis = null;
  try {
    const meta = await getJSON(base() + 'meta.json');
    const [cb, eb, gb, nav, an] = await Promise.all([
      getBin(base() + 'cells.bin'),
      getBin(base() + 'edges.bin').catch(() => null),
      meta.has.geo ? getBin(base() + 'geo.bin').catch(() => null) : null,
      meta.has.nav ? getJSON(base() + 'nav.json').catch(() => null) : null,
      meta.has.analysis ? getJSON(base() + 'analysis.json').catch(() => null) : null,
    ]);
    if (token !== S.loadToken) return;
    S.meta = meta;
    S.cells = parseCells(cb);
    S.edges = eb ? parseEdges(eb) : null;
    S.nav = nav;
    S.analysis = an;
    S.teamsOff.clear();
    S.classesOff.clear();
    S.linesOn.clear();
    buildChips();
    setupNavSources();
    if (gb) buildGeo(parseGeo(gb));
    setupCut();
    frame();
    rebuildAll();
    buildStats();
    status('');
  } catch (e) {
    if (token === S.loadToken) status('Could not load this map (' + e.message + ').');
  }
}

// ---------------------------------------------------------------- UI builders
function chip(label, { color, icon, off, onToggle, title }) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'chip' + (off ? ' off' : '');
  b.setAttribute('aria-pressed', off ? 'false' : 'true');
  if (title) b.title = title;
  if (icon && ICON_RE.test(icon)) {
    const img = document.createElement('img');
    img.src = 'icons/' + icon;
    img.alt = '';
    img.loading = 'lazy';
    b.append(img);
  } else if (color) {
    const i = document.createElement('i');
    i.style.background = COLOR_RE.test(color) ? color : '#9aa0a6';
    b.append(i);
  }
  const s = document.createElement('span');
  s.textContent = label;
  b.append(s);
  b.addEventListener('click', () => {
    const nowOff = !b.classList.contains('off');
    b.classList.toggle('off', nowOff);
    b.setAttribute('aria-pressed', nowOff ? 'false' : 'true');
    onToggle(!nowOff);
  });
  return b;
}

function teamList() {
  const t = S.meta.teams || {};
  return Object.keys(t).map(Number).filter((k) => k >= 0 && k < 16).sort((a, b) => a - b)
    .map((k) => ({ id: k, name: String(t[k].name || `Team ${k}`).slice(0, 40), color: COLOR_RE.test(t[k].color) ? t[k].color : '#9aa0a6' }));
}

function buildChips() {
  const tc = $('teamChips'), cc = $('classChips'), lc = $('lineChips');
  tc.replaceChildren(); cc.replaceChildren(); lc.replaceChildren();
  for (const t of teamList()) {
    const tchip = chip(t.name, { color: t.color, onToggle: (on) => { on ? S.teamsOff.delete(t.id) : S.teamsOff.add(t.id); rebuildHeat(); } });
    tchip.dataset.id = t.id;
    tc.append(tchip);
    lc.append(chip(t.name, { color: t.color, off: true, onToggle: (on) => { on ? S.linesOn.add(t.id) : S.linesOn.delete(t.id); rebuildLines(); } }));
  }
  const g = S.manifest.games[S.game] || {};
  const present = new Set(S.meta.classes || []);
  const all = (S.manifest.classes || []).filter((c) => present.has(c.key));
  for (const c of all) {
    const icon = (g.icons && g.icons[c.key]) || `${c.key}.png`;
    const cchip = chip(c.label, { icon, onToggle: (on) => { on ? S.classesOff.delete(c.id) : S.classesOff.add(c.id); rebuildHeat(); rebuildLines(); } });
    cchip.dataset.id = c.id;
    cc.append(cchip);
  }
  $('classChips').closest('section').hidden = all.length === 0;

}

function buildStats() {
  const m = S.meta;
  const h = (v) => (v / 3600).toFixed(1) + ' h';
  $('stats').textContent = `Human time ${h(m.samples.human)}, bot time ${h(m.samples.bot)}, deaths ${m.samples.deaths}. ` +
    `Updated ${new Date(m.updated * 1000).toLocaleString()}.`;
  const dl = $('dl');
  dl.replaceChildren();
  const files = ['meta.json', 'cells.bin', 'edges.bin'];
  if (m.has.nav) files.push('nav.json');
  if (m.has.analysis) files.push('analysis.json');
  for (const f of files) {
    const a = document.createElement('a');
    a.href = base() + f;
    a.textContent = f;
    a.download = `${S.game}_${S.map}_${f}`;
    dl.append(a);
  }
}

// ---------------------------------------------------------------- geometry
let geoBounds = null;
function buildGeo(g) {
  const geom = new THREE.BufferGeometry();
  geom.setAttribute('position', new THREE.BufferAttribute(g.pos, 3));
  geom.setIndex(new THREE.BufferAttribute(g.idx, 1));
  geom.computeBoundingBox();
  geoBounds = geom.boundingBox;
  // Walls (steep faces) are drawn two-sided so a wall seen from behind never looks like a hole;
  // floors and ceilings stay one-sided, so roofs seen from above stay see-through (dollhouse view).
  const P = g.pos, I = g.idx, flat = [], walls = [];
  for (let t = 0; t < I.length; t += 3) {
    const a = I[t] * 3, b = I[t + 1] * 3, c = I[t + 2] * 3;
    const ux = P[b] - P[a], uy = P[b + 1] - P[a + 1], uz = P[b + 2] - P[a + 2];
    const vx = P[c] - P[a], vy = P[c + 1] - P[a + 1], vz = P[c + 2] - P[a + 2];
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    const len = Math.hypot(nx, ny, nz) || 1;
    (Math.abs(nz / len) >= 0.7 ? flat : walls).push(I[t], I[t + 1], I[t + 2]);
  }
  const base = { color: 0xf5f5f2, flatShading: true, clippingPlanes: clipList, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 };
  for (const [ids, side] of [[flat, THREE.FrontSide], [walls, THREE.DoubleSide]]) {
    if (!ids.length) continue;
    const part = new THREE.BufferGeometry();
    part.setAttribute('position', geom.getAttribute('position'));
    part.setIndex(ids);
    groups.geo.add(new THREE.Mesh(part, new THREE.MeshLambertMaterial({ ...base, side })));
  }
  if (!MOBILE || g.idx.length < 300000) {
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geom, 35), new THREE.LineBasicMaterial({ color: 0xb9bcc2, clippingPlanes: clipList }));
    groups.geo.add(edges);
  }
  groups.geo.visible = $('showGeo').checked;
}

function dataBounds() {
  if (geoBounds) return geoBounds.clone();
  const c = S.cells, b = new THREE.Box3();
  for (let i = 0; i < c.len; i++) b.expandByPoint(new THREE.Vector3(c.x[i] * 32, c.y[i] * 32, c.z[i] * 32));
  if (b.isEmpty()) b.set(new THREE.Vector3(-512, -512, -64), new THREE.Vector3(512, 512, 64));
  return b;
}

function frame(top = true) {
  const b = dataBounds();
  const c = b.getCenter(new THREE.Vector3()), s = b.getSize(new THREE.Vector3());
  // distance that fits both the map's width and height in the current viewport
  const tanV = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  const fitH = (s.y / 2) / tanV, fitW = (s.x / 2) / (tanV * Math.max(0.2, camera.aspect));
  const d = Math.max(fitH, fitW) * 1.08 + s.z + 100;
  const r = d / 1.35;
  controls.target.copy(c);
  if (top) camera.position.set(c.x, c.y - 1, c.z + d);
  else camera.position.set(c.x - r * 0.7, c.y - r * 0.9, c.z + r * 0.9);
  camera.near = Math.max(4, d / 500);
  camera.far = d * 20;
  camera.updateProjectionMatrix();
  controls.update();
  redraw();
}

function setupCut() {
  const b = dataBounds();
  const el = $('cut');
  el.min = Math.floor(b.min.z);
  el.max = Math.ceil(b.max.z) + 1;
  el.value = el.max;
  applyCut();
}
function applyCut() {
  const el = $('cut');
  const v = Number(el.value);
  const off = v >= Number(el.max);
  clip.constant = off ? 1e9 : v;
  $('cutOut').textContent = off ? 'off' : String(v);
  redraw();
}

// ---------------------------------------------------------------- heat points
function passCommon(team, cls, bot) {
  if (bot ? !S.bots : !S.humans) return false;
  if (S.teamsOff.has(team) || S.classesOff.has(cls)) return false;
  return true;
}

function rebuildHeat() {
  disposeGroup(groups.heat);
  const c = S.cells;
  if (!c) return;
  const kinds = S.layer === 'deaths' ? [KIND_DEATH] : S.layer === 'air' ? [KIND_AIR] : (S.incAir ? [KIND_GROUND, KIND_AIR] : [KIND_GROUND]);
  const ks = new Set(kinds);
  const acc = new Map();
  const teamCol = Object.fromEntries(teamList().map((t) => [t.id, hexRgb(t.color)]));
  for (let i = 0; i < c.len; i++) {
    if (!ks.has(c.kind[i]) || !passCommon(c.team[i], c.cls[i], c.bot[i])) continue;
    const key = (c.x[i] + 2048) * 16777216 + (c.y[i] + 2048) * 4096 + (c.z[i] + 2048);
    let a = acc.get(key);
    if (!a) { a = [c.x[i], c.y[i], c.z[i], 0, 0, 0, 0]; acc.set(key, a); }
    const n = c.n[i];
    a[3] += n;
    const tc = teamCol[c.team[i]] || [0.6, 0.6, 0.6];
    a[4] += tc[0] * n; a[5] += tc[1] * n; a[6] += tc[2] * n;
  }
  let max = 1;
  for (const a of acc.values()) if (a[3] > max) max = a[3];
  const pos = new Float32Array(acc.size * 3), col = new Float32Array(acc.size * 3);
  let j = 0;
  const lmax = Math.log1p(max);
  for (const a of acc.values()) {
    pos[j * 3] = a[0] * 32 + 16; pos[j * 3 + 1] = a[1] * 32 + 16; pos[j * 3 + 2] = a[2] * 32 + 10;
    const t = Math.log1p(a[3]) / lmax;
    let rgb;
    if (S.colorMode === 'team') {
      const k = 0.35 + 0.65 * t;
      rgb = [a[4] / a[3] * k, a[5] / a[3] * k, a[6] / a[3] * k];
    } else rgb = ramp(0.08 + 0.92 * t);
    col[j * 3] = rgb[0]; col[j * 3 + 1] = rgb[1]; col[j * 3 + 2] = rgb[2];
    j++;
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('color', new THREE.BufferAttribute(col, 3));
  const mat = new THREE.PointsMaterial({ size: worldPt(38) * S.psize, sizeAttenuation: true, vertexColors: true, map: dot, transparent: true, alphaTest: 0.05, depthWrite: false, depthTest: !S.overWalls, clippingPlanes: clipList });
  const pts = new THREE.Points(g, mat);
  pts.userData.acc = [...acc.values()];
  pts.renderOrder = 2;
  groups.heat.add(pts);
  $('legend').hidden = S.colorMode !== 'heat' || acc.size === 0;
  $('legMin').textContent = '1';
  $('legMax').textContent = S.layer === 'deaths' ? `${max} deaths` : fmtTime(max);
  redraw();
}
function fmtTime(s) { return s >= 3600 ? (s / 3600).toFixed(1) + ' h' : s >= 60 ? Math.round(s / 60) + ' min' : s + ' s'; }

// ---------------------------------------------------------------- movement lines
function rebuildLines() {
  disposeGroup(groups.lines);
  const e = S.edges;
  if (!e || !S.linesOn.size) { redraw(); return; }
  const per = new Map();
  for (let i = 0; i < e.len; i++) {
    const t = e.team[i];
    if (!S.linesOn.has(t) || e.n[i] < S.lineMin || !passCommon(t, e.cls[i], e.bot[i])) continue;
    let arr = per.get(t);
    if (!arr) { arr = []; per.set(t, arr); }
    arr.push(i);
  }
  const teams = Object.fromEntries(teamList().map((t) => [t.id, hexRgb(t.color)]));
  for (const [t, ids] of per) {
    const pos = new Float32Array(ids.length * 6), col = new Float32Array(ids.length * 6);
    const rgb = teams[t] || [0.7, 0.7, 0.7];
    ids.forEach((i, j) => {
      pos.set([e.x[i] * 64 + 32, e.y[i] * 64 + 32, e.z[i] * 64 + 24, e.x2[i] * 64 + 32, e.y2[i] * 64 + 32, e.z2[i] * 64 + 24], j * 6);
      // dim at the start, full colour at the end: shows direction of travel
      col.set([rgb[0] * 0.25, rgb[1] * 0.25, rgb[2] * 0.25, rgb[0], rgb[1], rgb[2]], j * 6);
    });
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('color', new THREE.BufferAttribute(col, 3));
    const l = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.85, depthTest: false, clippingPlanes: clipList }));
    l.renderOrder = 3;
    groups.lines.add(l);
  }
  redraw();
}

// ---------------------------------------------------------------- nav overlays
function navCorners(a) {
  // [id, nwx, nwy, nwz, sex, sey, sez, nez, swz, attr]
  return [[a[1], a[2], a[3]], [a[4], a[2], a[7]], [a[4], a[5], a[6]], [a[1], a[5], a[8]]];
}
// ---- navigation sources: the map's .nav mesh and/or bot waypoint graphs (jk_botti, Parabot, FoXBot)
const SRC_RE = /^[a-z0-9_]{1,24}$/;
function setupNavSources() {
  S.navSources = [];
  if (S.nav) S.navSources.push({ key: 'nav', label: S.meta.navFormat === '.qnav' ? 'NextBot nav mesh (.qnav)' : 'Nav mesh (.nav)' });
  for (const w of (S.meta.has && S.meta.has.waypoints) || []) {
    if (w && SRC_RE.test(w.key)) S.navSources.push({ key: w.key, label: String(w.label || w.key).slice(0, 40) });
  }
  const sel = $('navSrc');
  sel.replaceChildren();
  for (const s of S.navSources) sel.append(new Option(s.label, s.key));
  if (!S.navSources.length) sel.append(new Option('none for this map', ''));
  S.navSrc = S.navSources.length ? S.navSources[0].key : null;
  sel.disabled = S.navSources.length < 2;
  selectNavSource();
}
async function selectNavSource() {
  const token = S.loadToken, key = S.navSrc;
  S.wp = null; S.curAnalysis = null;
  if (key === 'nav') S.curAnalysis = S.analysis;
  else if (key && SRC_RE.test(key)) {
    try {
      const [wp, an] = await Promise.all([getJSON(base() + `waypoints_${key}.json`), getJSON(base() + `analysis_${key}.json`).catch(() => null)]);
      if (token !== S.loadToken || key !== S.navSrc) return;
      S.wp = wp; S.curAnalysis = an;
    } catch { S.wp = null; }
  }
  const has = !!(key === 'nav' ? S.nav : S.wp);
  for (const id of ['showNav', 'showNavLinks', 'showGaps', 'showMissing']) $(id).disabled = !has;
  $('lblNav').textContent = key === 'nav' || !key ? 'Nav areas' : 'Waypoints';
  $('lblLinks').textContent = key === 'nav' || !key ? 'Area connections' : 'Waypoint links';
  $('navNote').textContent = !key ? 'No .nav file or bot waypoints for this map on the server.'
    : key === 'nav' ? `${S.nav.areas.length} areas (nav v${S.nav.version}.${S.nav.subversion})`
    : S.wp ? `${S.wp.nodes.length} waypoints, ${S.wp.links.length} links` : 'Could not load these waypoints.';
  rebuildNav();
  rebuildAnalysis();
}
function rebuildNav() {
  disposeGroup(groups.nav);
  disposeGroup(groups.navLinks);
  if (S.navSrc && S.navSrc !== 'nav') return rebuildWaypoints();
  if (!S.nav) return;
  if ($('showNav').checked) {
    const A = S.nav.areas;
    const pos = new Float32Array(A.length * 18), line = new Float32Array(A.length * 24);
    A.forEach((a, i) => {
      const c = navCorners(a).map((p) => [p[0], p[1], p[2] + 3]);
      pos.set([...c[0], ...c[1], ...c[2], ...c[0], ...c[2], ...c[3]], i * 18);
      line.set([...c[0], ...c[1], ...c[1], ...c[2], ...c[2], ...c[3], ...c[3], ...c[0]], i * 24);
    });
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    groups.nav.add(new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: 0x2fc6d9, transparent: true, opacity: 0.22, side: THREE.DoubleSide, depthWrite: false, clippingPlanes: clipList })));
    const lg = new THREE.BufferGeometry();
    lg.setAttribute('position', new THREE.BufferAttribute(line, 3));
    groups.nav.add(new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ color: 0x1696a8, clippingPlanes: clipList })));
  }
  if ($('showNavLinks').checked) {
    const A = S.nav.areas, center = new Map();
    for (const a of A) center.set(a[0], [(a[1] + a[4]) / 2, (a[2] + a[5]) / 2, (a[3] + a[6] + a[7] + a[8]) / 4 + 8]);
    const pts = [];
    A.forEach((a, i) => { for (const b of S.nav.conn[i] || []) { const cb = center.get(b); if (cb) pts.push(...center.get(a[0]), ...cb); } });
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pts), 3));
    groups.navLinks.add(new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0x7ee0ff, transparent: true, opacity: 0.5, clippingPlanes: clipList })));
  }
  redraw();
}
function rebuildWaypoints() {
  const W = S.wp;
  if (!W) { redraw(); return; }
  if ($('showNav').checked && W.nodes.length) {
    const pos = new Float32Array(W.nodes.length * 3);
    W.nodes.forEach((n, i) => pos.set([n[0], n[1], n[2]], i * 3));
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    const p = new THREE.Points(g, new THREE.PointsMaterial({ size: worldPt(22) * S.psize, color: 0x2fc6d9, map: dot, transparent: true, alphaTest: 0.05, depthTest: !S.overWalls, clippingPlanes: clipList }));
    p.renderOrder = 3;
    groups.nav.add(p);
  }
  if ($('showNavLinks').checked && W.links.length) {
    const pts = new Float32Array(W.links.length * 6);
    let j = 0;
    for (const [a, b] of W.links) {
      const na = W.nodes[a], nb = W.nodes[b];
      if (!na || !nb) continue;
      pts.set([na[0], na[1], na[2], nb[0], nb[1], nb[2]], j); j += 6;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pts.subarray(0, j), 3));
    const l = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0x7ee0ff, transparent: true, opacity: 0.6, depthTest: !S.overWalls, clippingPlanes: clipList }));
    l.renderOrder = 3;
    groups.navLinks.add(l);
  }
  redraw();
}
function rebuildAnalysis() {
  disposeGroup(groups.gaps);
  disposeGroup(groups.missing);
  const an = S.curAnalysis;
  if (!an) return;
  if ($('showGaps').checked && an.gaps.length) {
    const pos = new Float32Array(an.gaps.length * 3);
    an.gaps.forEach((g, i) => pos.set([g[0], g[1], g[2] + 12], i * 3));
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    const p = new THREE.Points(g, new THREE.PointsMaterial({ size: worldPt(40) * S.psize, color: 0xff2bd6, map: dot, transparent: true, alphaTest: 0.05, depthTest: false, clippingPlanes: clipList }));
    p.renderOrder = 4;
    groups.gaps.add(p);
  }
  if ($('showMissing').checked && an.missingLinks.length) {
    const pos = new Float32Array(an.missingLinks.length * 6);
    an.missingLinks.forEach((m, i) => pos.set([...m[3], ...m[4]], i * 6));
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    const l = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: 0xff8a00, depthTest: false, clippingPlanes: clipList }));
    l.renderOrder = 4;
    groups.missing.add(l);
  }
  redraw();
}

function rebuildAll() { rebuildHeat(); rebuildLines(); rebuildNav(); rebuildAnalysis(); }

// ---------------------------------------------------------------- picking
const ray = new THREE.Raycaster();
let downAt = null;
canvas.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY]; });
canvas.addEventListener('pointerup', (e) => {
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 6) return;
  const r = canvas.getBoundingClientRect();
  const v = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(v, camera);
  ray.params.Points.threshold = 20;
  const pts = groups.heat.children[0];
  const info = $('info');
  if (!pts) { info.hidden = true; return; }
  const hit = ray.intersectObject(pts)[0];
  if (!hit) { info.hidden = true; return; }
  const a = pts.userData.acc[hit.index];
  info.textContent = `Cell ${a[0] * 32}, ${a[1] * 32}, ${a[2] * 32} (32 units)\n` +
    (S.layer === 'deaths' ? `${a[3]} death(s)` : `${fmtTime(a[3])} of presence`) + ' with the current filters';
  info.hidden = false;
});

// ---------------------------------------------------------------- wiring
function status(t) { $('status').textContent = t; }

$('gameSel').addEventListener('change', (e) => { if (GAME_RE.test(e.target.value)) { S.game = e.target.value; fillMaps(null); } });
$('mapSel').addEventListener('change', (e) => { if (MAP_RE.test(e.target.value)) { S.map = e.target.value; loadMap(); if (MOBILE) setPanel(false); } });
for (const b of $('layerSeg').querySelectorAll('button')) b.addEventListener('click', () => {
  $('layerSeg').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
  S.layer = b.dataset.layer; rebuildHeat();
});
for (const b of $('colorSeg').querySelectorAll('button')) b.addEventListener('click', () => {
  $('colorSeg').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
  S.colorMode = b.dataset.color; rebuildHeat();
});
$('incAir').addEventListener('change', (e) => { S.incAir = e.target.checked; rebuildHeat(); });
$('showHumans').addEventListener('change', (e) => { S.humans = e.target.checked; rebuildHeat(); rebuildLines(); });
$('showBots').addEventListener('change', (e) => { S.bots = e.target.checked; rebuildHeat(); rebuildLines(); });
// "all": everything on -> everything off; otherwise -> everything on
function toggleAll(container, offSet) {
  const chips = [...$(container).querySelectorAll('.chip')];
  const allOn = chips.every((c) => !c.classList.contains('off'));
  offSet.clear();
  for (const c of chips) {
    c.classList.toggle('off', allOn);
    c.setAttribute('aria-pressed', allOn ? 'false' : 'true');
    if (allOn) offSet.add(Number(c.dataset.id));
  }
}
$('teamsAll').addEventListener('click', () => { toggleAll('teamChips', S.teamsOff); rebuildHeat(); });
$('classesAll').addEventListener('click', () => { toggleAll('classChips', S.classesOff); rebuildHeat(); rebuildLines(); });
$('lineMin').addEventListener('input', (e) => { S.lineMin = Number(e.target.value); $('lineMinOut').textContent = e.target.value; });
$('lineMin').addEventListener('change', () => rebuildLines());
for (const id of ['showNav', 'showNavLinks']) $(id).addEventListener('change', rebuildNav);
$('navSrc').addEventListener('change', (e) => { const v = e.target.value; if (S.navSources.some((s) => s.key === v)) { S.navSrc = v; selectNavSource(); } });
for (const id of ['showGaps', 'showMissing']) $(id).addEventListener('change', rebuildAnalysis);
$('showGeo').addEventListener('change', (e) => { groups.geo.visible = e.target.checked; redraw(); });
$('cut').addEventListener('input', applyCut);
$('overWalls').addEventListener('change', (e) => { S.overWalls = e.target.checked; rebuildHeat(); });
$('psize').addEventListener('input', (e) => { S.psize = Number(e.target.value) / 100; $('psizeOut').textContent = e.target.value + '%'; });
$('psize').addEventListener('change', () => { rebuildHeat(); rebuildNav(); rebuildAnalysis(); });
$('topBtn').addEventListener('click', () => frame(true));
$('persBtn').addEventListener('click', () => frame(false));

function setPanel(open) {
  document.body.classList.toggle('panel-open', open);
  redraw();
  $('menuBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
}
$('menuBtn').addEventListener('click', () => setPanel(!document.body.classList.contains('panel-open')));
window.addEventListener('hashchange', () => {
  const [g, m] = parseHash();
  if (g && m && (g !== S.game || m !== S.map) && S.manifest?.games[g]) {
    S.game = g; $('gameSel').value = g; fillMaps(m);
  }
});

resize();
loadManifest().catch((e) => status('Could not load the map list (' + e.message + ').'));
