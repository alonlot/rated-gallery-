// Gallery with filters, detail view, map and export. Works with two data providers:
//  - pcProvider(): the PC server (/api/index ...)
//  - phoneProvider(): the Android app bridge (window.RGBridge), offline, read only.
import { h, api, toast, scoreClass, zoomable, onSwipe, isTouch } from './util.js';

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
    rate: (faceId, rating) => api('/api/calib/rate', { method: 'POST', body: { face_id: faceId, rating, source: 'gallery' } }),
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
  return {
    kind: 'phone',
    mapEnabled: mapOn,
    setMap(v) { try { localStorage.setItem(key, v ? '1' : '0'); } catch (e) { /* ignore */ } },
    loadIndex: async () => JSON.parse(B.getIndex() || '{"photos":[],"persons":[]}'),
    thumbUrl: p => `/media/thumb/${encodeURIComponent(p.mid)}`,
    viewUrl: p => `/media/full/${encodeURIComponent(p.mid)}`,
    details: async p => ({ breakdown: p.bd || [], meta: p.meta || {}, score: p.s, conf: p.c, prior: p.prior,
      rating: p.r, me_face: null, faces: p.fb ? [{ x1: p.fb[0], y1: p.fb[1], x2: p.fb[2], y2: p.fb[3], name: 'Me', me: true }] : [] }),
    canRate: false,
    canExport: false,
    download: p => B.share(p.mid),
    downloadLabel: 'Share',
    search: async q => {
      if (!B.search) return { ok: false, reason: 'Update the phone app to use search.' };
      // The bridge call blocks while the model loads the first time: let "Searching…" show first.
      await new Promise(r => setTimeout(r, 50));
      try { return JSON.parse(B.search(q)); } catch (e) { return { ok: false, reason: String(e) }; }
    },
    status: () => { try { return JSON.parse(B.getStatus()); } catch (e) { return {}; } },
    sync: () => B.syncNow(),
    openPC: () => B.openPC(),
  };
}

const DEFAULT_F = {
  minScore: 0, hideLowConf: false, sort: 'score', includeNoMe: false,
  peopleMode: 'any', people: [], match: 'all',
  dateFrom: '', dateTo: '', year: '', month: '', tod: '', noDate: 'any',
  country: '', city: '', rLat: '', rLon: '', rKm: '', noLoc: 'any',
  camera: '', facing: '', npMin: '', npMax: '', src: '',
};

function loadFilters() {
  try { return { ...DEFAULT_F, ...JSON.parse(localStorage.getItem('rg_filters') || '{}') }; } catch (e) { return { ...DEFAULT_F }; }
}
function saveFilters(f) { try { localStorage.setItem('rg_filters', JSON.stringify(f)); } catch (e) { /* ignore */ } }

function km(lat1, lon1, lat2, lon2) {
  const p = Math.PI / 180;
  const a = Math.sin((lat2 - lat1) * p / 2) ** 2 + Math.cos(lat1 * p) * Math.cos(lat2 * p) * Math.sin((lon2 - lon1) * p / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(Math.min(1, a)));
}

// Pure filter function, exported for tests.
export function applyFilters(photos, f, meId) {
  const sel = (f.people || []).map(Number);
  const rLat = parseFloat(f.rLat), rLon = parseFloat(f.rLon), rKm = parseFloat(f.rKm);
  const useR = !isNaN(rLat) && !isNaN(rLon) && rKm > 0;
  const npMin = f.npMin === '' ? null : +f.npMin, npMax = f.npMax === '' ? null : +f.npMax;
  const out = photos.filter(p => {
    if (!f.includeNoMe && !p.me) return false;
    if (f.minScore > 0 && (p.s === null || p.s === undefined || p.s < f.minScore)) return false;
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
  if (f.sort === 'score') out.sort((a, b) => sNull(b.s) - sNull(a.s) || (b.t || '').localeCompare(a.t || ''));
  else if (f.sort === 'date') out.sort((a, b) => (b.t || '').localeCompare(a.t || ''));
  else if (f.sort === 'date_asc') out.sort((a, b) => (a.t || '9').localeCompare(b.t || '9'));
  return out;
}

export function createGallery(root, provider) {
  let index = { photos: [], persons: [] };
  let f = loadFilters();
  let list = [];
  let shown = 0;
  let selecting = false;
  const selected = new Set();
  let mode = 'grid';
  let map = null, mapLayer = null;
  let observer = null;
  let searchRes = null;   // Map photo id -> similarity, while a search is active
  let searchQ = '';
  const meId = () => (index.persons.find(p => p.me) || {}).id;

  root.innerHTML = '';
  const searchInput = h('input', { type: 'search', dir: 'auto', enterkeyhint: 'search', class: 'grow',
    placeholder: 'Search photos (any language): river, sunset…', 'aria-label': 'Search photos' });
  let searchTimer = null;
  searchInput.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch(searchInput.value), 500); });
  searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(searchTimer); runSearch(searchInput.value); searchInput.blur(); } });
  const searchBar = h('div', { class: 'row searchbar' }, searchInput,
    h('button', { onclick: () => { searchInput.value = ''; runSearch(''); } }, 'Clear'));
  root.append(searchBar);
  const top = h('div', { class: 'gal-top' });
  const filtersBox = h('details', { class: 'filters' });
  const info = h('div', { class: 'muted small', style: { margin: '4px 0 8px' } });
  const grid = h('div', { class: 'grid' });
  const sentinel = h('div', { style: { height: '1px' } });
  const mapBox = h('div', { id: 'map', style: { display: 'none' } });
  root.append(top, filtersBox, info, grid, sentinel, mapBox);

  async function reload() {
    try { index = await provider.loadIndex(); } catch (e) { info.textContent = 'Could not load photos: ' + e.message; return; }
    buildTop(); buildFilters(); refresh();
  }

  function setF(k, v) { f[k] = v; saveFilters(f); refresh(); }

  async function runSearch(q) {
    q = (q || '').trim();
    searchQ = q;
    if (!q) { searchRes = null; refresh(); return; }
    info.textContent = 'Searching…';
    const r = await provider.search(q);
    if (q !== searchQ) return;  // a newer search started
    if (!r.ok) { searchRes = null; refresh(); toast(r.reason || 'Search is not available'); return; }
    searchRes = new Map(r.results.map(([id, sim]) => [id, sim]));
    refresh();
    if (r.notIndexed) info.textContent += ` · ${r.notIndexed} new photos are not searchable yet`;
  }

  function buildTop() {
    top.innerHTML = '';
    const sortSel = h('select', { onchange: e => setF('sort', e.target.value), 'aria-label': 'Sort' },
      h('option', { value: 'score' }, 'Best score first'), h('option', { value: 'date' }, 'Newest first'), h('option', { value: 'date_asc' }, 'Oldest first'));
    sortSel.value = f.sort;
    top.append(sortSel);
    const views = h('div', { class: 'chips' },
      h('span', { class: 'chip' + (mode === 'grid' ? ' on' : ''), onclick: () => { mode = 'grid'; buildTop(); refresh(); } }, 'Grid'));
    if (provider.mapEnabled) views.append(h('span', { class: 'chip' + (mode === 'map' ? ' on' : ''), onclick: () => { mode = 'map'; buildTop(); refresh(); } }, 'Map'));
    top.append(views);
    if (provider.canExport) {
      top.append(h('button', { onclick: () => { selecting = !selecting; if (!selecting) selected.clear(); buildTop(); renderGrid(true); } }, selecting ? 'Cancel selection' : 'Select'));
      if (selecting) {
        top.append(h('button', { onclick: () => { list.forEach(p => selected.add(p.id)); buildTop(); renderGrid(true); } }, `Select all ${list.length}`));
        top.append(h('button', { class: 'primary', disabled: !selected.size, onclick: doExport }, `Copy ${selected.size} to folder`));
      }
    }
    if (provider.kind === 'phone') {
      const st = provider.status();
      top.append(h('span', { class: 'muted small' }, st.connected ? 'PC connected' : 'Offline (last ratings)'));
      top.append(h('button', { onclick: () => { provider.sync(); toast('Sync started'); } }, 'Sync'));
      top.append(h('button', { onclick: () => { provider.setMap(!provider.mapEnabled); provider.mapEnabled = !provider.mapEnabled; buildTop(); } }, provider.mapEnabled ? 'Map off' : 'Map on (needs internet)'));
    }
  }

  async function doExport() {
    const folder = prompt('Copy the selected originals to this folder on the PC (for example D:\\Best photos):');
    if (!folder) return;
    try { await provider.exportTo([...selected], folder); toast('Copying started. See Library for progress.'); selecting = false; selected.clear(); buildTop(); renderGrid(true); }
    catch (e) { toast(e.message); }
  }

  function opt(v, label, cur) { const o = h('option', { value: v }, label); if (String(cur) === String(v)) o.selected = true; return o; }

  function buildFilters() {
    const P = index.photos;
    const cnt = (key) => { const m = new Map(); for (const p of P) { if (p[key]) m.set(p[key], (m.get(p[key]) || 0) + 1); } return [...m.entries()].sort((a, b) => b[1] - a[1]); };
    const countries = new Map(); for (const p of P) if (p.cc) countries.set(p.cc, p.country || p.cc);
    const cities = cnt('city').filter(([c]) => !f.country || P.some(p => p.city === c && p.cc === f.country));
    const years = [...new Set(P.filter(p => p.t).map(p => p.t.slice(0, 4)))].sort().reverse();
    const others = index.persons.filter(p => !p.me);
    filtersBox.innerHTML = '';
    filtersBox.append(h('summary', {}, 'Filters', h('span', { class: 'muted small', style: { marginLeft: '8px' } }, activeCount() ? `(${activeCount()} on)` : '')));
    const g = h('div', { class: 'fgrid' });
    const field = (label, ...el) => h('div', {}, h('label', {}, label), ...el);
    const sel = (key, options) => { const s = h('select', { onchange: e => setF(key, e.target.value) }, ...options); s.value = f[key]; return s; };

    const minS = h('input', { type: 'range', min: 0, max: 10, step: 0.5, value: f.minScore, oninput: e => { f.minScore = +e.target.value; minLbl.textContent = f.minScore || 'any'; }, onchange: e => setF('minScore', +e.target.value) });
    const minLbl = h('b', {}, f.minScore || 'any');
    g.append(field('Minimum score: ', minLbl, minS));
    g.append(field('Confidence', h('div', { class: 'chips' }, h('span', { class: 'chip' + (f.hideLowConf ? ' on' : ''), onclick: () => { setF('hideLowConf', !f.hideLowConf); buildFilters(); } }, 'Hide low-confidence scores'))));
    g.append(field('Photos', h('div', { class: 'chips' },
      h('span', { class: 'chip' + (!f.includeNoMe ? ' on' : ''), onclick: () => { setF('includeNoMe', false); buildFilters(); } }, 'With me'),
      h('span', { class: 'chip' + (f.includeNoMe ? ' on' : ''), onclick: () => { setF('includeNoMe', true); buildFilters(); } }, 'All photos'))));

    const pm = h('div', { class: 'chips' },
      ...[['any', 'Anyone'], ['alone', 'Me alone'], ['with', 'With people']].map(([v, l]) =>
        h('span', { class: 'chip' + (f.peopleMode === v ? ' on' : ''), onclick: () => { setF('peopleMode', v); buildFilters(); } }, l)));
    const peopleBox = h('div', {}, pm);
    if (f.peopleMode === 'with') {
      if (!others.length) peopleBox.append(h('div', { class: 'muted small' }, 'Name people on the PC (Me & People) to use this filter.'));
      peopleBox.append(h('div', { class: 'chips', style: { marginTop: '6px' } }, ...others.map(p =>
        h('span', { class: 'chip' + (f.people.includes(p.id) ? ' on' : ''), onclick: () => { const s = new Set(f.people); s.has(p.id) ? s.delete(p.id) : s.add(p.id); setF('people', [...s]); buildFilters(); } }, p.name))));
      peopleBox.append(h('div', { class: 'chips', style: { marginTop: '6px' } }, ...[['all', 'All of them'], ['any', 'Any of them'], ['only', 'Only me and them']].map(([v, l]) =>
        h('span', { class: 'chip' + (f.match === v ? ' on' : ''), onclick: () => { setF('match', v); buildFilters(); } }, l))));
    }
    g.append(field('People', peopleBox));

    g.append(field('Year', sel('year', [opt('', 'Any year', f.year), ...years.map(y => opt(y, y, f.year))])));
    g.append(field('Month', sel('month', [opt('', 'Any month', f.month), ...['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].map((m, i) => opt(i + 1, m, f.month))])));
    g.append(field('Date range', h('div', { class: 'row' },
      h('input', { type: 'date', value: f.dateFrom, onchange: e => setF('dateFrom', e.target.value) }),
      h('input', { type: 'date', value: f.dateTo, onchange: e => setF('dateTo', e.target.value) }))));
    g.append(field('Time of day', sel('tod', [opt('', 'Any time', f.tod), opt('morning', 'Morning (5-12)', f.tod), opt('afternoon', 'Afternoon (12-17)', f.tod), opt('evening', 'Evening (17-21)', f.tod), opt('night', 'Night (21-5)', f.tod)])));
    g.append(field('No date', sel('noDate', [opt('any', 'Include photos with no date', f.noDate), opt('exclude', 'Hide photos with no date', f.noDate), opt('only', 'Only photos with no date', f.noDate)])));

    g.append(field('Country', sel('country', [opt('', 'Any country', f.country), ...[...countries.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([cc, n]) => opt(cc, n, f.country))])));
    g.append(field('City', sel('city', [opt('', 'Any city', f.city), ...cities.map(([c, n]) => opt(c, `${c} (${n})`, f.city))])));
    g.append(field('Radius around a point (lat, lon, km)', h('div', { class: 'row' },
      h('input', { type: 'text', inputmode: 'decimal', placeholder: 'lat', value: f.rLat, style: { width: '30%' }, onchange: e => setF('rLat', e.target.value) }),
      h('input', { type: 'text', inputmode: 'decimal', placeholder: 'lon', value: f.rLon, style: { width: '30%' }, onchange: e => setF('rLon', e.target.value) }),
      h('input', { type: 'number', placeholder: 'km', value: f.rKm, style: { width: '25%' }, onchange: e => setF('rKm', e.target.value) }))));
    g.append(field('No location', sel('noLoc', [opt('any', 'Include photos with no location', f.noLoc), opt('exclude', 'Hide photos with no location', f.noLoc), opt('only', 'Only photos with no location', f.noLoc)])));

    g.append(field('Camera', sel('camera', [opt('', 'Any camera', f.camera), ...cnt('cam').map(([c, n]) => opt(c, `${c} (${n})`, f.camera))])));
    g.append(field('Camera side', sel('facing', [opt('', 'Front and rear', f.facing), opt('front', 'Front (selfie)', f.facing), opt('rear', 'Rear', f.facing), opt('unknown', 'Unknown', f.facing)])));
    g.append(field('Number of people', h('div', { class: 'row' },
      h('input', { type: 'number', min: 0, placeholder: 'min', value: f.npMin, style: { width: '45%' }, onchange: e => setF('npMin', e.target.value) }),
      h('input', { type: 'number', min: 0, placeholder: 'max', value: f.npMax, style: { width: '45%' }, onchange: e => setF('npMax', e.target.value) }))));
    if (provider.kind === 'pc') g.append(field('Source', sel('src', [opt('', 'Phone and folders', f.src), opt('phone', 'Phone', f.src), opt('folder', 'Folders (Takeout)', f.src)])));
    g.append(h('div', {}, h('label', {}, ' '), h('button', { onclick: () => { f = { ...DEFAULT_F }; saveFilters(f); buildFilters(); refresh(); } }, 'Clear all filters')));
    filtersBox.append(g);
  }

  function activeCount() {
    return Object.keys(DEFAULT_F).filter(k => k !== 'sort' && JSON.stringify(f[k]) !== JSON.stringify(DEFAULT_F[k])).length;
  }

  function refresh() {
    list = applyFilters(index.photos, f, meId());
    if (searchRes) list = list.filter(p => searchRes.has(p.id)).sort((a, b) => searchRes.get(b.id) - searchRes.get(a.id));
    const scored = list.filter(p => p.s !== null && p.s !== undefined).length;
    const m = index.model || {};
    info.textContent = `${list.length} ${list.length === 1 ? 'photo' : 'photos'}` + (scored ? `, ${scored} with my score` : '') +
      (m.trained ? ` · model from ${m.labels} ratings` : (index.photos.some(p => p.prior) ? ' · scores are estimates until you rate photos' : ''));
    if (searchRes) info.textContent = `${list.length} best matches for "${searchQ}", best match first` +
      (f.includeNoMe ? '' : ' · only photos with me (change in Filters > Photos)');
    const sum = filtersBox.querySelector('summary span'); if (sum) sum.textContent = activeCount() ? `(${activeCount()} on)` : '';
    if (mode === 'map') { grid.style.display = 'none'; mapBox.style.display = ''; renderMap(); }
    else { mapBox.style.display = 'none'; grid.style.display = ''; renderGrid(true); }
  }

  function tile(p) {
    const lowc = p.c !== null && p.c !== undefined && p.c < LOW_CONF;
    const el = h('div', { class: 'tile' + (selected.has(p.id) ? ' sel' : ''), 'data-id': p.id },
      h('img', { src: provider.thumbUrl(p), loading: 'lazy', alt: '' }),
      p.s !== null && p.s !== undefined ? h('span', { class: `badge ${scoreClass(p.s)}${lowc ? ' lowconf' : ''}`, title: lowc ? 'Low confidence' : '' }, (p.prior ? '~' : '') + p.s.toFixed(1)) : null,
      selecting ? h('span', { class: 'chk' }) : null);
    el.addEventListener('click', () => {
      if (selecting) { selected.has(p.id) ? selected.delete(p.id) : selected.add(p.id); el.classList.toggle('sel'); buildTop(); }
      else openDetail(list.indexOf(p));
    });
    return el;
  }

  function renderGrid(reset) {
    if (reset) { grid.innerHTML = ''; shown = 0; }
    const end = Math.min(list.length, shown + PAGE);
    const frag = document.createDocumentFragment();
    for (let i = shown; i < end; i++) frag.append(tile(list[i]));
    grid.append(frag);
    shown = end;
    if (!observer) {
      observer = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting) && shown < list.length && mode === 'grid') renderGrid(false); }, { rootMargin: '800px' });
      observer.observe(sentinel);
    }
  }

  function renderMap() {
    if (!window.L) { mapBox.textContent = 'Map library missing.'; return; }
    if (!map) {
      map = L.map(mapBox, { preferCanvas: true }).setView([30, 10], 2);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
      map.on('contextmenu', e => { f.rLat = e.latlng.lat.toFixed(5); f.rLon = e.latlng.lng.toFixed(5); if (!f.rKm) f.rKm = '25'; saveFilters(f); buildFilters(); refresh(); toast('Radius center set'); });
    }
    setTimeout(() => map.invalidateSize(), 50);
    if (mapLayer) mapLayer.remove();
    mapLayer = L.layerGroup().addTo(map);
    const pts = [];
    const byScore = list.map((p, i) => [p, i]).sort((a, b) => (a[0].s ?? -1) - (b[0].s ?? -1));  // best drawn last = on top
    byScore.forEach(([p, i]) => {
      if (p.lat === null || p.lat === undefined) return;
      pts.push([p.lat, p.lon]);
      const col = p.s >= 8 ? '#1f8a4c' : p.s >= 5 ? '#b7791f' : p.s ? '#c0392b' : '#666';
      L.circleMarker([p.lat, p.lon], { radius: 7, color: '#fff', weight: 1, fillColor: col, fillOpacity: 0.9 }).on('click', () => openDetail(i)).addTo(mapLayer);
    });
    if (pts.length) map.fitBounds(pts, { maxZoom: 13, padding: [20, 20] });
    info.textContent += ` · ${pts.length} on the map (long press or right click sets the radius center)`;
  }

  // ------------------------------------------------------------ detail view
  function openDetail(i) {
    if (i < 0 || i >= list.length) return;
    let cur = i;
    let showBox = false;
    const modal = h('div', { class: 'modal', role: 'dialog' });
    const img = h('img', { alt: '' });
    const stage = h('div', { class: 'stage' }, img);
    const side = h('div', { class: 'side' });
    const counter = h('span', { class: 'grow small' });
    const close = () => { modal.remove(); document.removeEventListener('keydown', onKey); if (history.state && history.state.rgModal) history.back(); };
    const go = d => { if (cur + d >= 0 && cur + d < list.length) { cur += d; show(); } };
    modal.append(h('div', { class: 'mtop' },
      h('button', { onclick: close, 'aria-label': 'Close' }, '✕'), counter,
      h('button', { onclick: () => { showBox = !showBox; drawBox(); }, title: 'Show my face' }, '▢'),
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
      const p = list[cur];
      z.reset();
      counter.textContent = `${cur + 1} / ${list.length}`;
      img.src = provider.thumbUrl(p);
      const full = new Image();
      full.onload = () => { if (list[cur] === p) { img.src = full.src; setTimeout(drawBox, 30); } };
      full.src = provider.viewUrl(p);
      side.innerHTML = '';
      side.append(h('div', { class: 'muted small' }, 'Loading…'));
      try { det = await provider.details(p); } catch (e) { side.textContent = e.message; return; }
      if (list[cur] !== p) return;
      side.innerHTML = '';
      const sc = det.score ?? p.s;
      if (p.me) {
        side.append(h('div', { style: { fontSize: '28px', fontWeight: 700 } }, sc !== null && sc !== undefined ? (p.prior || det.prior ? '~' : '') + sc.toFixed(1) : '–',
          h('span', { class: 'small muted', style: { fontWeight: 400, marginLeft: '8px' } }, 'my score')));
        const cf = det.conf ?? p.c;
        if (cf !== null && cf !== undefined) side.append(h('div', { class: 'small', style: { color: cf < LOW_CONF ? '#ff8a7a' : '#aaa' } }, `Confidence ${Math.round(cf * 100)}%` + (cf < LOW_CONF ? ' (face small, blurred or turned away)' : '')));
        if (p.prior || det.prior) side.append(h('div', { class: 'small', style: { color: '#aaa' } }, 'Estimate from simple rules. Rate photos to train your own model.'));
        const bd = det.breakdown || [];
        if (bd.length) {
          side.append(h('h3', {}, 'What changes my score'));
          const mx = Math.max(0.5, ...bd.map(x => Math.abs(x[1])));
          side.append(h('div', { class: 'bd' }, ...bd.flatMap(([name, v]) => [
            h('span', {}, name, h('span', { style: { color: v >= 0 ? '#47c27a' : '#ff6b5b', marginLeft: '6px' } }, (v >= 0 ? '+' : '') + v.toFixed(1))),
            h('span', { class: 'b' }, h('i', { style: { left: v >= 0 ? '50%' : (50 - 50 * Math.abs(v) / mx) + '%', width: (50 * Math.abs(v) / mx) + '%', background: v >= 0 ? '#47c27a' : '#ff6b5b' } }))])));
        }
        if (provider.canRate && det.me_face) {
          side.append(h('h3', {}, 'My rating', det.rating ? `: ${det.rating}` : ''));
          const r10 = h('div', { class: 'rate10' }, ...Array.from({ length: 10 }, (_, k) => h('button', { class: det.rating === k + 1 ? 'cur' : '', onclick: async () => {
            try { await provider.rate(det.me_face, k + 1); p.r = k + 1; det.rating = k + 1; toast(`Rated ${k + 1}. The model uses it at the next training.`); r10.querySelectorAll('button').forEach((b, j) => b.classList.toggle('cur', j === k)); } catch (e) { toast(e.message); }
          } }, String(k + 1))));
          side.append(r10);
        }
      } else {
        side.append(h('div', { class: 'muted' }, 'I am not in this photo. No score.'));
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

  if (provider.kind === 'phone') window.RG_onIndexUpdated = () => reload();
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
