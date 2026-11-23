// Gallery: search, quick chips, filters sheet, grid (bursts collapsed, month headers), trips and events, map,
// detail view with rating. Works with two data providers:
//  - pcProvider(): the PC server (/api/index ...)
//  - phoneProvider(): the Android app bridge (window.RGBridge), offline. Ratings wait in a queue on the phone.
import { h, api, toast, scoreClass, zoomable, onSwipe } from './util.js';
import { collapseBursts, findTrips, monthLabel, km } from './groups.js';

const PAGE = 120;
const LOW_CONF = 0.4;
const TOD = { morning: [5, 12], afternoon: [12, 17], evening: [17, 21], night: [21, 29] };

export function pcProvider(settings = {}) {
  return {
    kind: 'pc',
    mapEnabled: !!settings.map_enabled,
    loadIndex: () => api('/api/index'),
    thumbUrl: p => `/api/photo/${p.id}/thumb`,
    viewUrl: p => `/api/photo/${p.id}/view`,
    details: p => api(`/api/photo/${p.id}`),
    canRate: true,
    rate: (p, det, rating) => api('/api/calib/rate', { method: 'POST', body: { face_id: det.me_face, rating, source: 'gallery' } }),
    pending: () => ({}),
    canExport: true,
    exportTo: (ids, folder) => api('/api/export', { method: 'POST', body: { photo_ids: ids, folder } }),
    download: p => { location.href = `/api/photo/${p.id}/original`; },
    downloadLabel: 'Download original',
    search: async q => {
      try { const r = await api(`/api/search?q=${encodeURIComponent(q)}`); return { ok: true, results: r.results, notIndexed: r.not_indexed }; }
      catch (e) { return { ok: false, reason: e.message }; }
    },
  };
}

export function phoneProvider() {
  const B = window.RGBridge;
  const key = 'rg_map_enabled';
  let mapOn = false;
  try { mapOn = localStorage.getItem(key) === '1'; } catch (e) { /* storage may be blocked */ }
  const parse = (s, d) => { try { return JSON.parse(s); } catch (e) { return d; } };
  return {
    kind: 'phone',
    mapEnabled: mapOn,
    setMap(v) { try { localStorage.setItem(key, v ? '1' : '0'); } catch (e) { /* ignore */ } },
    loadIndex: async () => parse(B.getIndex(), { photos: [], persons: [] }),
    thumbUrl: p => `/media/thumb/${encodeURIComponent(p.mid)}`,
    viewUrl: p => `/media/full/${encodeURIComponent(p.mid)}`,
    details: async p => ({ breakdown: p.bd || [], meta: p.meta || {}, score: p.s, score_p: p.sp, conf: p.c, prior: p.prior,
      rating: p.r, me_face: null, faces: p.fb ? [{ x1: p.fb[0], y1: p.fb[1], x2: p.fb[2], y2: p.fb[3], name: 'Me', me: true }] : [] }),
    canRate: !!(B && B.rate),
    rate: async (p, det, rating) => {
      const r = parse(B.rate(p.id, rating), { ok: false, reason: 'error' });
      if (!r.ok) throw new Error(r.reason || 'Rating was not saved');
      return r;
    },
    pending: () => (B && B.getPendingRatings ? parse(B.getPendingRatings(), {}) : {}),
    canExport: false,
    download: p => B.share(p.mid),
    downloadLabel: 'Share',
    search: async q => {
      if (!B.search) return { ok: false, reason: 'Update the phone app to use search.' };
      await new Promise(r => setTimeout(r, 50));  // the first call blocks while the model loads: show "Searching…" first
      return parse(B.search(q), { ok: false, reason: 'Search failed' });
    },
    status: () => parse(B.getStatus(), {}),
    sync: () => B.syncNow(),
    openPC: () => B.openPC(),
  };
}

const DEFAULT_F = {
  minScore: 0, hideLowConf: false, sort: 'score', includeNoMe: false, bursts: true, scoreMode: 'all',
  peopleMode: 'any', people: [], match: 'all',
  dateFrom: '', dateTo: '', year: '', month: '', tod: '', noDate: 'any',
  country: '', city: '', rLat: '', rLon: '', rKm: '', noLoc: 'any',
  camera: '', facing: '', npMin: '', npMax: '', src: '',
};
const VIEW_KEYS = ['sort', 'bursts', 'scoreMode'];  // display options, not counted as filters

function loadFilters() {
  try { return { ...DEFAULT_F, ...JSON.parse(localStorage.getItem('rg_filters') || '{}') }; } catch (e) { return { ...DEFAULT_F }; }
}
function saveFilters(f) { try { localStorage.setItem('rg_filters', JSON.stringify(f)); } catch (e) { /* ignore */ } }

export const scoreKey = f => (f.scoreMode === 'period' ? 'sp' : 's');

// Pure filter function, exported for tests.
export function applyFilters(photos, f, meId) {
  const sk = scoreKey(f);
  const sel = (f.people || []).map(Number);
  const rLat = parseFloat(f.rLat), rLon = parseFloat(f.rLon), rKm = parseFloat(f.rKm);
  const useR = !isNaN(rLat) && !isNaN(rLon) && rKm > 0;
  const npMin = f.npMin === '' || f.npMin === undefined ? null : +f.npMin, npMax = f.npMax === '' || f.npMax === undefined ? null : +f.npMax;
  const out = photos.filter(p => {
    if (!f.includeNoMe && !p.me) return false;
    if (f.minScore > 0 && (p[sk] === null || p[sk] === undefined || p[sk] < f.minScore)) return false;
    if (f.hideLowConf && p.me && (p.c === null || p.c < LOW_CONF)) return false;
    const pp = p.p || [];
    if (f.peopleMode === 'alone') { if (!p.me || (p.np || 0) > 1 || pp.some(x => x !== meId)) return false; }
    else if (f.peopleMode === 'with' && sel.length) {
      if (f.match === 'all' && !sel.every(x => pp.includes(x))) return false;
      if (f.match === 'any' && !sel.some(x => pp.includes(x))) return false;
      if (f.match === 'only' && (!sel.every(x => pp.includes(x)) || pp.some(x => x !== meId && !sel.includes(x)))) return false;
    }
    const t = p.t;
    if (f.noDate === 'only' && t) return false;
    if (f.noDate === 'exclude' && !t) return false;
    if (f.dateFrom || f.dateTo || f.year || f.month || f.tod) {
      if (!t) return false;
      if (f.dateFrom && t.slice(0, 10) < f.dateFrom) return false;
      if (f.dateTo && t.slice(0, 10) > f.dateTo) return false;
      if (f.year && t.slice(0, 4) !== String(f.year)) return false;
      if (f.month && +t.slice(5, 7) !== +f.month) return false;
      if (f.tod) { let hr = +t.slice(11, 13); const [a, b] = TOD[f.tod]; if (hr < 5) hr += 24; if (hr < a || hr >= b) return false; }
    }
    const hasLoc = p.lat !== null && p.lat !== undefined;
    if (f.noLoc === 'only' && hasLoc) return false;
    if (f.noLoc === 'exclude' && !hasLoc) return false;
    if (f.country || f.city || useR) {
      if (!hasLoc) return false;
      if (f.country && p.cc !== f.country) return false;
      if (f.city && p.city !== f.city) return false;
      if (useR && km(rLat, rLon, p.lat, p.lon) > rKm) return false;
    }
    if (f.camera && p.cam !== f.camera) return false;
    if (f.facing && p.facing !== f.facing) return false;
    if (npMin !== null && (p.np || 0) < npMin) return false;
    if (npMax !== null && (p.np || 0) > npMax) return false;
    if (f.src && p.src !== f.src) return false;
    return true;
  });
  const sNull = v => (v === null || v === undefined ? -1 : v);
  if (f.sort === 'score') out.sort((a, b) => sNull(b[sk]) - sNull(a[sk]) || (b.t || '').localeCompare(a.t || ''));
  else if (f.sort === 'date') out.sort((a, b) => (b.t || '').localeCompare(a.t || ''));
  else if (f.sort === 'date_asc') out.sort((a, b) => (a.t || '9').localeCompare(b.t || '9'));
  else if (f.sort === 'attr') out.sort((a, b) => sNull(b.a) - sNull(a.a) || sNull(b[sk]) - sNull(a[sk]));
  return out;
}

const fmt = v => (v === null || v === undefined ? '–' : v.toFixed(1));

export function createGallery(root, provider) {
  let index = { photos: [], persons: [] };
  let byId = new Map();
  let f = loadFilters();
  let list = [], others = new Map(), shown = 0, lastMonth = null;
  let mode = 'photos';
  let group = null;                 // a selected trip or event: { title, ids: Set }
  const expanded = new Set();       // burst ids opened in the grid
  let selecting = false;
  const selected = new Set();
  let searchRes = null, searchQ = '';
  let pending = {};
  let tripsCache = null;
  let statusChip = null;      // phone: the "PC connected / Offline" chip, updated in place
  let reloadWaiting = false;  // phone: a new index arrived while the user was busy; shown as a chip
  let map = null, mapLayer = null, observer = null;
  const meId = () => (index.persons.find(p => p.me) || {}).id;
  const sk = () => scoreKey(f);

  root.innerHTML = '';
  root.classList.add('gallery');
  const searchInput = h('input', { type: 'search', dir: 'auto', enterkeyhint: 'search', placeholder: 'Search photos (any language)', 'aria-label': 'Search photos' });
  const filterBtn = h('button', { class: 'filterbtn', onclick: () => openSheet() }, 'Filters');
  const chipRow = h('div', { class: 'chiprow' });
  const appbar = h('div', { class: 'appbar' }, h('div', { class: 'searchrow' }, h('div', { class: 'searchbox' }, h('span', { class: 'sicon', 'aria-hidden': 'true' }, '⌕'), searchInput), filterBtn), chipRow);
  const groupBar = h('div', { class: 'groupbar', style: { display: 'none' } });
  const info = h('div', { class: 'info' });
  const grid = h('div', { class: 'grid' });
  const sentinel = h('div', { style: { height: '1px' } });
  const tripsBox = h('div', { class: 'trips', style: { display: 'none' } });
  const mapBox = h('div', { id: 'map', style: { display: 'none' } });
  root.append(appbar, groupBar, info, grid, sentinel, tripsBox, mapBox);

  let searchTimer = null;
  searchInput.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch(searchInput.value), 500); });
  searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(searchTimer); runSearch(searchInput.value); searchInput.blur(); } });

  async function reload() {
    try { index = await provider.loadIndex(); } catch (e) { info.textContent = 'Could not load photos: ' + e.message; return; }
    byId = new Map(index.photos.map(p => [p.id, p]));
    tripsCache = null;
    reloadWaiting = false;
    applyPending();
    buildChips(); refresh();
  }

  // The phone saves a new index during long syncs. Reloading redraws the grid from the top, so do it at once only
  // when the user is at the top of the grid and not in a photo, a menu or a sheet; otherwise offer it as a chip.
  function userBusy() {
    const a = document.activeElement;
    return window.scrollY > 200 || !!document.querySelector('.modal, .sheet') || (a && (a.tagName === 'SELECT' || a.tagName === 'INPUT'));
  }
  function onIndexUpdated() {
    if (userBusy()) { reloadWaiting = true; buildChips(); } else reload();
  }
  function statusText(st) { return st.connected ? '● PC connected' : '○ Offline'; }

  function applyPending() {
    pending = provider.pending() || {};
    for (const [id, v] of Object.entries(pending)) { const p = byId.get(+id); if (p) { p.r = v.rating; p.rPending = true; } }
  }

  function setF(k, v) { f[k] = v; saveFilters(f); refresh(); }

  async function runSearch(q) {
    q = (q || '').trim();
    searchQ = q;
    if (!q) { searchRes = null; refresh(); return; }
    info.textContent = 'Searching…';
    const r = await provider.search(q);
    if (q !== searchQ) return;
    if (!r.ok) { searchRes = null; refresh(); toast(r.reason || 'Search is not available'); return; }
    searchRes = new Map(r.results.map(([id, sim]) => [id, sim]));
    if (mode === 'trips') mode = 'photos';
    buildChips(); refresh();
    if (r.notIndexed) info.textContent += ` · ${r.notIndexed} new photos are not searchable yet`;
  }

  // ------------------------------------------------------------ top bar
  const chip = (label, on, onclick, title) => h('button', { class: 'chip' + (on ? ' on' : ''), onclick, title, 'aria-pressed': on ? 'true' : 'false' }, label);

  function buildChips() {
    chipRow.innerHTML = '';
    const seg = h('div', { class: 'seg' },
      ...[['photos', 'Photos'], ['trips', 'Trips'], ...(provider.mapEnabled ? [['map', 'Map']] : [])].map(([m, l]) =>
        h('button', { class: mode === m ? 'on' : '', onclick: () => { mode = m; buildChips(); refresh(); } }, l)));
    chipRow.append(seg);
    const sort = h('select', { class: 'chipselect', 'aria-label': 'Sort', onchange: e => setF('sort', e.target.value) },
      h('option', { value: 'score' }, 'Best first'), h('option', { value: 'date' }, 'Newest'), h('option', { value: 'date_asc' }, 'Oldest'),
      h('option', { value: 'attr' }, 'Attractiveness'));
    sort.value = f.sort;
    if (!searchRes && mode === 'photos') chipRow.append(sort);
    chipRow.append(
      chip('Best of bursts', f.bursts, () => { setF('bursts', !f.bursts); buildChips(); }, 'Show only the best photo of each burst'),
      chip('Per period', f.scoreMode === 'period', () => { setF('scoreMode', f.scoreMode === 'period' ? 'all' : 'period'); buildChips(); }, 'Compare each photo only with photos from the same years'),
      chip(f.includeNoMe ? 'Everyone' : 'With me', f.includeNoMe, () => { setF('includeNoMe', !f.includeNoMe); buildChips(); }, 'Photos with me, or all photos'),
      chip('Selfies', f.facing === 'front', () => { setF('facing', f.facing === 'front' ? '' : 'front'); buildChips(); }),
      chip('8+', f.minScore >= 8, () => { setF('minScore', f.minScore >= 8 ? 0 : 8); buildChips(); }),
      chip('Me alone', f.peopleMode === 'alone', () => { setF('peopleMode', f.peopleMode === 'alone' ? 'any' : 'alone'); buildChips(); }));
    if (provider.canExport) {
      chipRow.append(chip(selecting ? 'Cancel select' : 'Select', selecting, () => { selecting = !selecting; if (!selecting) selected.clear(); buildChips(); renderGrid(true); }));
      if (selecting) {
        chipRow.append(chip(`All ${list.length}`, false, () => { list.forEach(p => selected.add(p.id)); buildChips(); renderGrid(true); }),
          h('button', { class: 'chip primary', disabled: !selected.size, onclick: doExport }, `Copy ${selected.size}`));
      }
    }
    if (provider.kind === 'phone') {
      const st = provider.status() || {};
      if (reloadWaiting) chipRow.append(chip('Updated scores ↻', true, () => { reload(); window.scrollTo(0, 0); }, 'New scores from the PC. Tap to show them.'));
      statusChip = chip(statusText(st), false, () => { provider.sync(); toast('Sync started'); }, 'Tap to sync');
      chipRow.append(statusChip,
        chip(provider.mapEnabled ? 'Map off' : 'Map on', false, () => { provider.mapEnabled = !provider.mapEnabled; provider.setMap(provider.mapEnabled); if (!provider.mapEnabled && mode === 'map') mode = 'photos'; buildChips(); refresh(); }));
    }
    const n = activeCount();
    filterBtn.textContent = n ? `Filters · ${n}` : 'Filters';
    filterBtn.classList.toggle('on', n > 0);
  }

  async function doExport() {
    const folder = prompt('Copy the selected originals to this folder on the PC (for example D:\\Best photos):');
    if (!folder) return;
    try { await provider.exportTo([...selected], folder); toast('Copying started. See Library for progress.'); selecting = false; selected.clear(); buildChips(); renderGrid(true); }
    catch (e) { toast(e.message); }
  }

  function activeCount() {
    return Object.keys(DEFAULT_F).filter(k => !VIEW_KEYS.includes(k) && JSON.stringify(f[k]) !== JSON.stringify(DEFAULT_F[k])).length;
  }

  // ------------------------------------------------------------ filters sheet
  function opt(v, label, cur) { const o = h('option', { value: v }, label); if (String(cur) === String(v)) o.selected = true; return o; }

  function openSheet() {
    const backdrop = h('div', { class: 'sheet-backdrop', onclick: () => close() });
    const body = h('div', { class: 'sheet-body' });
    const showBtn = h('button', { class: 'primary grow', onclick: () => close() });
    const sheet = h('div', { class: 'sheet', role: 'dialog', 'aria-label': 'Filters' },
      h('div', { class: 'sheet-head' }, h('b', { class: 'grow' }, 'Filters'), h('button', { onclick: () => close(), 'aria-label': 'Close' }, '✕')),
      body, h('div', { class: 'sheet-foot' }, h('button', { onclick: () => { const keep = VIEW_KEYS.map(k => [k, f[k]]); f = { ...DEFAULT_F, ...Object.fromEntries(keep) }; saveFilters(f); draw(); refresh(); } }, 'Clear all'), showBtn));
    document.body.append(backdrop, sheet);
    function close() { backdrop.remove(); sheet.remove(); buildChips(); }
    const upd = () => { showBtn.textContent = `Show ${list.length} ${list.length === 1 ? 'photo' : 'photos'}`; };
    const set = (k, v) => { setF(k, v); draw(); };
    function draw() {
      const P = index.photos;
      const cnt = key => { const m = new Map(); for (const p of P) if (p[key]) m.set(p[key], (m.get(p[key]) || 0) + 1); return [...m.entries()].sort((a, b) => b[1] - a[1]); };
      const countries = new Map(); for (const p of P) if (p.cc) countries.set(p.cc, p.country || p.cc);
      const cities = cnt('city').filter(([c]) => !f.country || P.some(p => p.city === c && p.cc === f.country));
      const years = [...new Set(P.filter(p => p.t).map(p => p.t.slice(0, 4)))].sort().reverse();
      const people = index.persons.filter(p => !p.me);
      const sel = (key, options) => { const s = h('select', { onchange: e => set(key, e.target.value) }, ...options); s.value = f[key]; return s; };
      const sec = (title, ...kids) => h('section', {}, h('h3', {}, title), ...kids);
      const chips = (pairs, key) => h('div', { class: 'chips' }, ...pairs.map(([v, l]) => chip(l, f[key] === v, () => set(key, v))));
      body.innerHTML = '';
      const minLbl = h('b', {}, f.minScore || 'any');
      body.append(sec('Score',
        h('div', { class: 'row' }, h('span', { class: 'grow' }, 'Minimum score: ', minLbl)),
        h('input', { type: 'range', min: 0, max: 10, step: 0.5, value: f.minScore, oninput: e => { minLbl.textContent = +e.target.value || 'any'; }, onchange: e => set('minScore', +e.target.value) }),
        h('div', { class: 'chips' }, chip('Hide low-confidence scores', f.hideLowConf, () => set('hideLowConf', !f.hideLowConf)))));
      const pbox = h('div', {}, chips([['any', 'Anyone'], ['alone', 'Me alone'], ['with', 'With people']], 'peopleMode'));
      if (f.peopleMode === 'with') {
        if (!people.length) pbox.append(h('p', { class: 'muted small' }, 'Name people on the PC (Me & People) to use this filter.'));
        pbox.append(h('div', { class: 'chips' }, ...people.map(p => chip(p.name, f.people.includes(p.id), () => { const s = new Set(f.people); s.has(p.id) ? s.delete(p.id) : s.add(p.id); set('people', [...s]); }))),
          chips([['all', 'All of them'], ['any', 'Any of them'], ['only', 'Only me and them']], 'match'));
      }
      body.append(sec('People', pbox, h('div', { class: 'row' }, h('span', { class: 'grow muted small' }, 'Number of people'),
        h('input', { type: 'number', min: 0, placeholder: 'min', value: f.npMin, style: { width: '80px' }, onchange: e => set('npMin', e.target.value) }),
        h('input', { type: 'number', min: 0, placeholder: 'max', value: f.npMax, style: { width: '80px' }, onchange: e => set('npMax', e.target.value) }))));
      body.append(sec('When',
        h('div', { class: 'row' }, sel('year', [opt('', 'Any year', f.year), ...years.map(y => opt(y, y, f.year))]),
          sel('month', [opt('', 'Any month', f.month), ...['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].map((m, i) => opt(i + 1, m, f.month))])),
        h('div', { class: 'row' }, h('input', { type: 'date', value: f.dateFrom, 'aria-label': 'From', onchange: e => set('dateFrom', e.target.value) }), '–',
          h('input', { type: 'date', value: f.dateTo, 'aria-label': 'To', onchange: e => set('dateTo', e.target.value) })),
        chips([['', 'Any time'], ['morning', 'Morning'], ['afternoon', 'Afternoon'], ['evening', 'Evening'], ['night', 'Night']], 'tod'),
        sel('noDate', [opt('any', 'Include photos with no date', f.noDate), opt('exclude', 'Hide photos with no date', f.noDate), opt('only', 'Only photos with no date', f.noDate)])));
      body.append(sec('Where',
        h('div', { class: 'row' }, sel('country', [opt('', 'Any country', f.country), ...[...countries.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([cc, n]) => opt(cc, n, f.country))]),
          sel('city', [opt('', 'Any city', f.city), ...cities.map(([c, n]) => opt(c, `${c} (${n})`, f.city))])),
        h('div', { class: 'row' }, h('span', { class: 'muted small' }, 'Radius'),
          h('input', { type: 'text', inputmode: 'decimal', placeholder: 'lat', value: f.rLat, style: { width: '90px' }, onchange: e => set('rLat', e.target.value) }),
          h('input', { type: 'text', inputmode: 'decimal', placeholder: 'lon', value: f.rLon, style: { width: '90px' }, onchange: e => set('rLon', e.target.value) }),
          h('input', { type: 'number', placeholder: 'km', value: f.rKm, style: { width: '70px' }, onchange: e => set('rKm', e.target.value) })),
        sel('noLoc', [opt('any', 'Include photos with no location', f.noLoc), opt('exclude', 'Hide photos with no location', f.noLoc), opt('only', 'Only photos with no location', f.noLoc)])));
      body.append(sec('Camera',
        h('div', { class: 'row' }, sel('camera', [opt('', 'Any camera', f.camera), ...cnt('cam').map(([c, n]) => opt(c, `${c} (${n})`, f.camera))]),
          sel('facing', [opt('', 'Front and rear', f.facing), opt('front', 'Front (selfie)', f.facing), opt('rear', 'Rear', f.facing), opt('unknown', 'Unknown', f.facing)])),
        provider.kind === 'pc' ? sel('src', [opt('', 'Phone and folders', f.src), opt('phone', 'Phone', f.src), opt('folder', 'Folders (Takeout)', f.src)]) : null));
      upd();
    }
    draw();
  }

  // ------------------------------------------------------------ list
  function refresh() {
    const key = sk();
    let base = applyFilters(index.photos, f, meId());
    if (group) base = base.filter(p => group.ids.has(p.id));
    if (searchRes) base = base.filter(p => searchRes.has(p.id)).sort((a, b) => searchRes.get(b.id) - searchRes.get(a.id));
    if (f.bursts) {
      const c = collapseBursts(base, key);
      others = c.others;
      list = [];
      for (const p of c.items) { list.push(p); if (p.b !== null && p.b !== undefined && expanded.has(p.b) && others.has(p.b)) list.push(...others.get(p.b)); }
    } else { list = base; others = new Map(); }
    groupBar.style.display = group ? '' : 'none';
    if (group) { groupBar.innerHTML = ''; groupBar.append(h('button', { onclick: () => { group = null; mode = 'trips'; buildChips(); refresh(); } }, '← Trips'), h('b', { class: 'grow' }, group.title), h('button', { onclick: () => { group = null; refresh(); }, 'aria-label': 'Close' }, '✕')); }
    const scored = list.filter(p => p[key] !== null && p[key] !== undefined).length;
    const hidden = base.length - list.length;
    const m = index.model || {};
    let txt = searchRes ? `${list.length} best matches for "${searchQ}"` : `${list.length} ${list.length === 1 ? 'photo' : 'photos'}`;
    if (hidden > 0) txt += ` (${hidden} more in bursts)`;
    if (scored && !searchRes) txt += f.scoreMode === 'period' ? ' · score per period' : '';
    if (!m.trained && index.photos.some(p => p.prior)) txt += ' · scores are estimates until you rate photos';
    if (searchRes && !f.includeNoMe) txt += ' · only photos with me (tap "With me" for everyone)';
    info.textContent = txt;
    grid.style.display = mode === 'photos' ? '' : 'none';
    tripsBox.style.display = mode === 'trips' ? '' : 'none';
    mapBox.style.display = mode === 'map' ? '' : 'none';
    if (mode === 'photos') renderGrid(true);
    else if (mode === 'trips') renderTrips();
    else renderMap();
  }

  function tile(p) {
    const key = sk();
    const s = p[key];
    const lowc = p.c !== null && p.c !== undefined && p.c < LOW_CONF;
    const nOthers = p.b !== null && p.b !== undefined && others.has(p.b) ? others.get(p.b).length : 0;
    const el = h('div', { class: 'tile' + (selected.has(p.id) ? ' sel' : ''), 'data-id': p.id },
      h('img', { src: provider.thumbUrl(p), loading: 'lazy', alt: '', decoding: 'async' }),
      s !== null && s !== undefined ? h('span', { class: `badge ${scoreClass(s)}${lowc ? ' lowconf' : ''}`, title: lowc ? 'Low confidence' : '' }, (p.prior ? '~' : '') + s.toFixed(1)) : null,
      nOthers && !expanded.has(p.b) ? h('button', { class: 'stack', title: `${nOthers} more in this burst`, onclick: e => { e.stopPropagation(); expanded.add(p.b); refresh(); } }, `+${nOthers}`) : null,
      nOthers && expanded.has(p.b) ? h('button', { class: 'stack open', title: 'Close the burst', onclick: e => { e.stopPropagation(); expanded.delete(p.b); refresh(); } }, '−') : null,
      p.rPending ? h('span', { class: 'pend', title: `My rating ${p.r}, sends at next sync` }, `✎${p.r}`) : null,
      selecting ? h('span', { class: 'chk' }) : null);
    el.addEventListener('click', () => {
      if (selecting) { selected.has(p.id) ? selected.delete(p.id) : selected.add(p.id); el.classList.toggle('sel'); buildChips(); }
      else openDetail(list.indexOf(p));
    });
    return el;
  }

  function renderGrid(reset) {
    if (reset) { grid.innerHTML = ''; shown = 0; lastMonth = null; }
    const headers = !searchRes && (f.sort === 'date' || f.sort === 'date_asc');
    const end = Math.min(list.length, shown + PAGE);
    const frag = document.createDocumentFragment();
    for (let i = shown; i < end; i++) {
      const p = list[i];
      if (headers) { const ml = monthLabel(p.t); if (ml !== lastMonth) { frag.append(h('div', { class: 'month' }, ml)); lastMonth = ml; } }
      frag.append(tile(p));
    }
    grid.append(frag);
    shown = end;
    if (!list.length) grid.append(h('div', { class: 'empty' }, searchRes ? 'No matches. Try other words, or tap "With me" to search all photos.' : 'No photos match. Open Filters and clear some of them.'));
    if (!observer) {
      observer = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting) && shown < list.length && mode === 'photos') renderGrid(false); }, { rootMargin: '1200px' });
      observer.observe(sentinel);
    }
  }

  // ------------------------------------------------------------ trips and events
  function renderTrips() {
    const key = sk();
    if (!tripsCache || tripsCache.key !== key) tripsCache = { key, r: findTrips(index.photos, { scoreKey: key }) };
    const { home, trips, events } = tripsCache.r;
    const visible = new Set(applyFilters(index.photos, f, meId()).map(p => p.id));
    tripsBox.innerHTML = '';
    const card = t => {
      const ids = t.ids.filter(i => visible.has(i));
      if (!ids.length) return null;
      const ps = ids.map(i => byId.get(i)).sort((a, b) => (b.me - a.me) || ((b[key] ?? -1) - (a[key] ?? -1)));
      const me = ps.filter(p => p.me);
      const best = me.length ? Math.max(...me.map(p => p[key] ?? -1)) : null;
      return h('button', { class: 'tripcard', onclick: () => { group = { title: `${t.title} · ${t.when}`, ids: new Set(t.ids) }; mode = 'photos'; buildChips(); refresh(); window.scrollTo(0, 0); } },
        h('div', { class: 'mosaic' + (ps.length < 3 ? ' one' : '') }, ...ps.slice(0, 3).map(p => h('img', { src: provider.thumbUrl(p), loading: 'lazy', alt: '' }))),
        h('div', { class: 'tripinfo' }, h('div', { class: 'triptitle' }, t.title), h('div', { class: 'muted small' }, t.when + (t.kind === 'trip' ? ` · ${t.days} ${t.days === 1 ? 'day' : 'days'}` : '')),
          h('div', { class: 'small' }, `${ids.length} photos · ${me.length} of me`, best !== null && best >= 0 ? h('span', { class: `badge inline ${scoreClass(best)}` }, `best ${best.toFixed(1)}`) : null)));
    };
    const shownCount = {};
    const section = (title, items, note) => {
      const cards = items.map(card).filter(Boolean);
      shownCount[title] = cards.length;
      tripsBox.append(h('h2', {}, title, h('span', { class: 'muted small' }, ` ${cards.length}`)));
      if (!cards.length) tripsBox.append(h('p', { class: 'muted' }, note));
      else tripsBox.append(h('div', { class: 'tripgrid' }, ...cards));
    };
    tripsBox.append(h('p', { class: 'muted small' }, home ? `Home: ${home.city}. A trip is photos more than 80 km from home.` : 'Trips need photos with a location. Events still work.'));
    section('Trips', trips, home ? 'No trips found with these filters.' : 'No home location yet.');
    section('Events', events, 'No events found (an event is 6 or more photos taken within a few hours).');
    const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
    info.textContent = `${plural(shownCount.Trips, 'trip')} and ${plural(shownCount.Events, 'event')}` +
      (shownCount.Trips < trips.length || shownCount.Events < events.length ? ' with these filters' : '');
  }

  // ------------------------------------------------------------ map
  function renderMap() {
    if (!window.L) { mapBox.textContent = 'Map library missing.'; return; }
    if (!map) {
      map = L.map(mapBox, { preferCanvas: true }).setView([30, 10], 2);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
      map.on('contextmenu', e => { f.rLat = e.latlng.lat.toFixed(5); f.rLon = e.latlng.lng.toFixed(5); if (!f.rKm) f.rKm = '25'; saveFilters(f); buildChips(); refresh(); toast('Radius center set'); });
    }
    setTimeout(() => map.invalidateSize(), 50);
    if (mapLayer) mapLayer.remove();
    mapLayer = L.layerGroup().addTo(map);
    const key = sk(), pts = [];
    const byScore = list.map((p, i) => [p, i]).sort((a, b) => (a[0][key] ?? -1) - (b[0][key] ?? -1));  // best drawn last = on top
    byScore.forEach(([p, i]) => {
      if (p.lat === null || p.lat === undefined) return;
      pts.push([p.lat, p.lon]);
      const s = p[key];
      const col = s >= 8 ? '#1f8a4c' : s >= 5 ? '#b7791f' : s ? '#c0392b' : '#777';
      L.circleMarker([p.lat, p.lon], { radius: 7, color: '#fff', weight: 1, fillColor: col, fillOpacity: 0.9 }).on('click', () => openDetail(i)).addTo(mapLayer);
    });
    if (pts.length) map.fitBounds(pts, { maxZoom: 13, padding: [20, 20] });
    info.textContent += ` · ${pts.length} on the map (long press sets the radius center)`;
  }

  // ------------------------------------------------------------ detail view
  function openDetail(i) {
    if (i < 0 || i >= list.length) return;
    let cur = i, extra = null;   // extra: a burst photo opened from the strip (not in the list)
    let showBox = false;
    const modal = h('div', { class: 'modal', role: 'dialog' });
    const img = h('img', { alt: '' });
    const stage = h('div', { class: 'stage' }, img);
    const side = h('div', { class: 'side' });
    const counter = h('span', { class: 'grow small' });
    const close = () => { modal.remove(); document.removeEventListener('keydown', onKey); if (history.state && history.state.rgModal) history.back(); };
    const go = d => { extra = null; if (cur + d >= 0 && cur + d < list.length) { cur += d; show(); } };
    modal.append(h('div', { class: 'mtop' },
      h('button', { onclick: close, 'aria-label': 'Close' }, '✕'), counter,
      h('button', { onclick: () => { showBox = !showBox; drawBox(); }, title: 'Show my face', 'aria-label': 'Show my face' }, '▢'),
      h('button', { onclick: () => go(-1), 'aria-label': 'Previous' }, '‹'),
      h('button', { onclick: () => go(1), 'aria-label': 'Next' }, '›')),
    h('div', { class: 'mbody' }, stage, side));
    document.body.append(modal);
    history.pushState({ rgModal: true }, '');
    const onPop = () => { if (document.body.contains(modal)) { modal.remove(); document.removeEventListener('keydown', onKey); } window.removeEventListener('popstate', onPop); };
    window.addEventListener('popstate', onPop);
    const z = zoomable(stage, img);
    onSwipe(stage, dir => go(dir === 'left' ? 1 : -1), () => z.zoomed);
    const onKey = e => { if (e.key === 'Escape') close(); if (e.key === 'ArrowRight') go(1); if (e.key === 'ArrowLeft') go(-1); };
    document.addEventListener('keydown', onKey);
    let det = null;

    function drawBox() {
      stage.querySelectorAll('.facebox').forEach(b => b.remove());
      if (!showBox || !det || !img.naturalWidth) return;
      const r = img.getBoundingClientRect(), sr = stage.getBoundingClientRect();
      for (const fc of det.faces || []) {
        if (!(fc.me || (det.me_face && fc.id === det.me_face))) continue;
        stage.append(h('div', { class: 'facebox', style: { left: (r.left - sr.left + fc.x1 * r.width) + 'px', top: (r.top - sr.top + fc.y1 * r.height) + 'px', width: ((fc.x2 - fc.x1) * r.width) + 'px', height: ((fc.y2 - fc.y1) * r.height) + 'px' } }));
      }
    }

    async function show() {
      const p = extra || list[cur];
      z.reset();
      counter.textContent = extra ? 'Burst photo' : `${cur + 1} / ${list.length}`;
      img.src = provider.thumbUrl(p);
      const full = new Image();
      full.onload = () => { if ((extra || list[cur]) === p) { img.src = full.src; setTimeout(drawBox, 30); } };
      full.src = provider.viewUrl(p);
      side.innerHTML = '';
      side.append(h('div', { class: 'muted small' }, 'Loading…'));
      try { det = await provider.details(p); } catch (e) { side.textContent = e.message; return; }
      if ((extra || list[cur]) !== p) return;
      side.innerHTML = '';
      if (p.me) {
        const all = det.score ?? p.s, per = det.score_p ?? p.sp;
        const main = f.scoreMode === 'period' ? per : all;
        side.append(h('div', { class: 'bigscore' }, main !== null && main !== undefined ? (p.prior ? '~' : '') + main.toFixed(1) : '–',
          h('span', {}, f.scoreMode === 'period' ? 'my score in this period' : 'my score')),
          h('div', { class: 'small muted' }, f.scoreMode === 'period' ? `All time: ${fmt(all)}` : `Compared with the same years: ${fmt(per)}`));
        if (p.a !== null && p.a !== undefined) {
          const d = p.ad ?? 0;
          side.append(h('div', { class: 'attr' }, h('b', {}, `General attractiveness ${p.a.toFixed(1)}/10`),
            h('span', { class: d > 0.05 ? 'up' : d < -0.05 ? 'down' : 'same' }, d > 0.05 ? ` +${d.toFixed(1)} above my average` : d < -0.05 ? ` ${d.toFixed(1)} below my average` : ' same as my average'),
            h('div', { class: 'small muted' }, 'Estimate of a general model (MEBeauty / SCUT-FBP5500), not my own taste.')));
        }
        const cf = det.conf ?? p.c;
        if (cf !== null && cf !== undefined) side.append(h('div', { class: 'small', style: { color: cf < LOW_CONF ? '#ff8a7a' : '#aaa' } }, `Confidence ${Math.round(cf * 100)}%` + (cf < LOW_CONF ? ' (face small, blurred or turned away)' : '')));
        if (p.prior || det.prior) side.append(h('div', { class: 'small', style: { color: '#aaa' } }, 'Estimate from simple rules. Rate photos to train your own model.'));
        if (provider.canRate) {
          const curR = p.rPending ? p.r : (det.rating ?? p.r);
          const note = h('div', { class: 'small muted' }, p.rPending ? 'Saved on the phone. It goes to the PC at the next sync.' : '');
          const r10 = h('div', { class: 'rate10' }, ...Array.from({ length: 10 }, (_, k) => h('button', { class: curR === k + 1 ? 'cur' : '', onclick: async () => {
            try {
              await provider.rate(p, det, k + 1);
              p.r = k + 1; det.rating = k + 1;
              if (provider.kind === 'phone') { p.rPending = true; note.textContent = 'Saved on the phone. It goes to the PC at the next sync.'; }
              else toast(`Rated ${k + 1}. The model uses it at the next training.`);
              r10.querySelectorAll('button').forEach((b, j) => b.classList.toggle('cur', j === k));
            } catch (e) { toast(e.message); }
          } }, String(k + 1))));
          side.append(h('h3', {}, 'My rating'), r10, note);
        }
        const bd = det.breakdown || [];
        if (bd.length) {
          side.append(h('h3', {}, 'What changes my score'));
          const mx = Math.max(0.5, ...bd.map(x => Math.abs(x[1])));
          side.append(h('div', { class: 'bd' }, ...bd.flatMap(([name, v]) => [
            h('span', {}, name, h('span', { style: { color: v >= 0 ? '#47c27a' : '#ff6b5b', marginLeft: '6px' } }, (v >= 0 ? '+' : '') + v.toFixed(1))),
            h('span', { class: 'b' }, h('i', { style: { left: v >= 0 ? '50%' : (50 - 50 * Math.abs(v) / mx) + '%', width: (50 * Math.abs(v) / mx) + '%', background: v >= 0 ? '#47c27a' : '#ff6b5b' } }))])));
        }
      } else {
        side.append(h('div', { class: 'muted' }, 'I am not in this photo. No score.'));
      }
      if (p.b !== null && p.b !== undefined) {
        const burst = index.photos.filter(x => x.b === p.b).sort((a, b) => (b[sk()] ?? -1) - (a[sk()] ?? -1));
        if (burst.length > 1) {
          side.append(h('h3', {}, `Burst: ${burst.length} photos`), h('div', { class: 'burststrip' }, ...burst.map(x =>
            h('button', { class: 'bthumb' + (x === p ? ' on' : ''), onclick: () => { extra = list.includes(x) ? null : x; if (!extra) cur = list.indexOf(x); show(); } },
              h('img', { src: provider.thumbUrl(x), alt: '' }), x[sk()] !== null && x[sk()] !== undefined ? h('span', { class: `badge ${scoreClass(x[sk()])}` }, x[sk()].toFixed(1)) : null))));
        }
      }
      const names = (det.faces || []).filter(x => x.name).map(x => x.name);
      if (names.length) side.append(h('h3', {}, 'People'), h('div', {}, [...new Set(names)].join(', ')));
      side.append(h('h3', {}, 'Details'));
      side.append(h('table', { class: 'meta' }, ...Object.entries(det.meta || {}).map(([k, v]) => h('tr', {}, h('td', {}, k), h('td', {}, String(v))))));
      if (det.features && Object.keys(det.features).length) {
        side.append(h('details', {}, h('summary', { class: 'small' }, 'Measured values'),
          h('table', { class: 'meta' }, ...Object.entries(det.features).map(([k, v]) => h('tr', {}, h('td', {}, k), h('td', {}, String(v)))))));
      }
      if (det.files && det.files.length) {
        side.append(h('details', {}, h('summary', { class: 'small' }, `Files (${det.files.length})`),
          h('table', { class: 'meta' }, ...det.files.map(x => h('tr', {}, h('td', {}, x.source), h('td', {}, x.rel + (x.status === 'missing' ? ' (missing)' : '')))))));
      }
      side.append(h('div', { class: 'row', style: { marginTop: '12px' } }, h('button', { onclick: () => provider.download(p) }, provider.downloadLabel)));
    }
    show();
  }

  if (provider.kind === 'phone') {
    window.RG_onIndexUpdated = () => onIndexUpdated();
    // Status arrives every few seconds, more often during a sync. Rebuilding the chips here closed the open sort
    // menu (the <select> was replaced), so only the text of the status chip changes.
    window.RG_onStatus = st => {
      const text = statusText(st || provider.status() || {});
      if (statusChip && statusChip.textContent !== text) statusChip.textContent = text;
    };
  }
  reload();
  return { reload };
}

// Standalone page (phone app WebView or /static/gallery.html on the PC)
export async function bootStandalone() {
  const root = document.getElementById('gallery');
  if (window.RGBridge) createGallery(root, phoneProvider());
  else {
    let s = {};
    try { s = await api('/api/settings'); } catch (e) { /* ignore */ }
    createGallery(root, pcProvider(s));
  }
}
