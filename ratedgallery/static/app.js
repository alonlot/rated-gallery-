// PC web UI (also used on the phone browser and in the phone app's "PC" tab).
import { h, api, fmtEta, toast, scoreClass, onSwipe } from './util.js';
import { createGallery, pcProvider } from './gallery.js';

const view = document.getElementById('view');
let status = null;
let timer = null;
let keyHandler = null;

const ROUTES = { library: renderLibrary, people: renderPeople, calibrate: renderCalibrate, gallery: renderGallery, settings: renderSettings };

function route() {
  const name = (location.hash.replace('#/', '') || 'library').split('/')[0];
  document.querySelectorAll('nav#nav a').forEach(a => a.classList.toggle('on', a.dataset.r === name));
  if (keyHandler) { document.removeEventListener('keydown', keyHandler); keyHandler = null; }
  clearInterval(timer); timer = null;
  view.innerHTML = '';
  (ROUTES[name] || renderLibrary)();
}
window.addEventListener('hashchange', route);

async function loadStatus() { status = await api('/api/status'); return status; }

// ------------------------------------------------------------------ Library
function taskCard(st) {
  const t = st.tasks.current;
  const card = h('div', { class: 'card' });
  if (!t) {
    card.append(h('div', { class: 'row' }, h('b', { class: 'grow' }, 'No task is running.'),
      st.tasks.queued.length ? h('span', { class: 'muted' }, 'Queued: ' + st.tasks.queued.join(', ')) : null));
    const last = st.tasks.history[0];
    if (last) card.append(h('div', { class: 'small ' + (last.status === 'error' ? 'err' : 'muted') }, `Last: ${last.name} ${last.status} in ${last.seconds} s. ${last.message || ''} ${last.error || ''}`));
    return card;
  }
  const pct = t.total ? Math.min(100, 100 * t.done / t.total) : 0;
  card.append(h('div', { class: 'row' }, h('b', { class: 'grow' }, t.phase || t.name),
    h('button', { class: 'danger', onclick: async () => { await api('/api/tasks/cancel', { method: 'POST' }); toast('Stopping…'); } }, 'Stop')));
  card.append(h('div', { class: 'bar' }, h('i', { style: { width: pct + '%' } })));
  card.append(h('div', { class: 'small muted', style: { marginTop: '6px' } },
    t.total ? `${t.done} of ${t.total}` : `${t.done}`, t.rate_per_min ? ` · ${Math.round(t.rate_per_min)} per minute` : '',
    t.eta_s ? ` · about ${fmtEta(t.eta_s)} left` : '', t.message ? ` · ${t.message}` : ''));
  return card;
}

async function renderLibrary() {
  view.append(h('h1', {}, 'Library'));
  const box = h('div');
  view.append(box);
  const draw = async () => {
    let st;
    try { st = await loadStatus(); } catch (e) { box.textContent = e.message; return; }
    box.innerHTML = '';
    if (st.missing_models.length) box.append(h('div', { class: 'card warn' }, h('b', {}, 'Models are missing: '), st.missing_models.join(', '), '. Run install.bat (needs internet one time).'));
    if (st.restart_needed) box.append(h('div', { class: 'card warn' }, 'The server is restarting with new network settings. Reload this page in a few seconds.'));
    box.append(taskCard(st));
    const s = st.stats;
    box.append(h('div', { class: 'card' }, h('div', { class: 'kv' },
      h('span', {}, 'Photos'), h('b', {}, s.photos),
      h('span', {}, 'Photos of me'), h('b', {}, `${s.me_photos} (${s.me_analyzed} analyzed, ${s.scored} scored)`),
      h('span', {}, 'Faces'), h('b', {}, s.faces),
      h('span', {}, 'Files'), h('span', {}, Object.entries(s.files).map(([k, v]) => `${v} ${k}`).join(', ') || '0'),
      h('span', {}, 'Model'), h('span', {}, st.model.message),
      h('span', {}, 'Preview cache'), h('span', {}, `${s.previews_mb} MB of ${st.settings.preview_cache_mb} MB`))));

    const src = h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, 'Photo sources'));
    for (const x of st.sources) {
      if (!x.enabled) continue;
      src.append(h('div', { class: 'row' }, h('span', { class: 'grow' }, x.kind === 'device' ? `📱 ${x.name}` : `📁 ${x.root}`),
        x.kind === 'folder' ? h('button', { onclick: async () => { if (confirm('Stop using this folder? Its photos stay in the database.')) { await api(`/api/sources/${x.id}`, { method: 'DELETE' }); draw(); } } }, 'Remove') : null));
    }
    const inp = h('input', { type: 'text', placeholder: 'Folder, for example D:\\Takeout', class: 'grow' });
    src.append(h('div', { class: 'row', style: { marginTop: '8px' } }, inp,
      h('button', { onclick: async () => { try { await api('/api/sources', { method: 'POST', body: { path: inp.value } }); toast('Folder added'); draw(); } catch (e) { toast(e.message); } } }, 'Add folder')));
    src.append(h('div', { class: 'row', style: { marginTop: '8px' } },
      h('button', { class: 'primary', onclick: async () => { try { await api('/api/scan', { method: 'POST' }); toast('Scan started'); draw(); } catch (e) { toast(e.message); } } }, 'Scan for new photos'),
      h('label', { class: 'chip' + (st.settings.low_priority ? ' on' : ''), onclick: async () => { await api('/api/settings', { method: 'POST', body: { low_priority: !st.settings.low_priority } }); draw(); } }, 'Low priority (use the PC while it scans)')));
    src.append(h('div', { class: 'small muted', style: { marginTop: '6px' } }, 'The app only reads your photos. It never changes, moves or deletes them.'));
    box.append(src);

    const devs = h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, 'Phone'));
    if (!st.devices.length) devs.append(h('div', { class: 'muted' }, 'No phone is paired. Go to Settings to pair the phone app.'));
    for (const d of st.devices) devs.append(h('div', {}, h('b', {}, d.name), ' ', h('span', { class: d.connected ? '' : 'muted' }, d.connected ? '● connected' : 'not connected'),
      h('span', { class: 'muted small' }, ` · ${d.n_files} photos · ${d.n_wanted} waiting for upload`)));
    box.append(devs);

    const b = st.benchmark;
    const bm = h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, 'Speed on this PC'));
    if (b && !b.error) {
      bm.append(h('div', { class: 'kv' },
        h('span', {}, 'Scan (all photos)'), h('b', {}, `about ${b.scan_photos_per_min} photos per minute (limit: ${b.scan_limit})`),
        h('span', {}, 'Analysis (photos of me)'), h('b', {}, `about ${b.features_photos_per_min} photos per minute`),
        h('span', {}, 'Details'), h('span', { class: 'small' }, `decode ${b.decode_ms} ms/photo/worker, detect ${b.detect_ms} ms, ArcFace ${b.arcface_ms_per_face} ms/face, CLIP ${b.clip_ms_per_image} ms, TOPIQ ${b.iqa_ms} ms, ${b.workers} workers`)));
    } else bm.append(h('div', { class: 'muted' }, b && b.error ? b.error : 'The benchmark runs at start.'));
    const dev = Object.entries(st.models || {});
    if (dev.length) bm.append(h('div', { class: 'small', style: { marginTop: '8px' } }, 'Devices: ', dev.map(([k, v]) => `${k}: ${v.device}${v.error ? ' (DirectML failed: ' + v.error + ')' : ''}`).join(' · ')));
    bm.append(h('button', { style: { marginTop: '8px' }, onclick: async () => { await api('/api/tasks/benchmark', { method: 'POST' }); toast('Benchmark queued'); } }, 'Run benchmark again'));
    box.append(bm);
  };
  await draw();
  timer = setInterval(() => { if (document.visibilityState === 'visible') draw(); }, 2000);
}

// ------------------------------------------------------------------ People
const faceImg = id => h('img', { src: `/api/face/${id}/img`, loading: 'lazy', alt: '' });

function selectableFaces(ids, sel, onChange) {
  return h('div', { class: 'faces' }, ...ids.map(id => {
    const el = h('div', { class: 'face' + (sel.has(id) ? ' sel' : '') }, faceImg(id));
    el.addEventListener('click', () => { sel.has(id) ? sel.delete(id) : sel.add(id); el.classList.toggle('sel'); onChange && onChange(); });
    return el;
  }));
}

async function renderPeople() {
  const parts = location.hash.split('/');
  if (parts[2] === 'person') return renderPerson(+parts[3]);
  if (parts[2] === 'cluster') return renderCluster(+parts[3]);
  view.append(h('h1', {}, 'Me & People'));
  const [pp, cl] = await Promise.all([api('/api/people'), api('/api/clusters')]);
  const me = pp.persons.find(p => p.is_me);
  if (!me) {
    const sel = new Set();
    const btn = h('button', { class: 'primary', disabled: true }, 'These are me');
    const upd = () => { btn.disabled = sel.size < 1; btn.textContent = `These are me (${sel.size})`; };
    btn.onclick = async () => { await api('/api/persons', { method: 'POST', body: { name: 'Me', face_ids: [...sel], is_me: true } }); toast('Saved. Matching your faces…'); route(); };
    view.append(h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, 'Step 1: select 5 to 10 faces of yourself'),
      h('p', { class: 'muted' }, 'Pick faces from different years, light and angles. Groups below are faces that look alike.'),
      h('div', { class: 'row' }, btn)));
    if (!cl.clusters.length && !cl.unclustered.length) view.append(h('div', { class: 'card' }, 'No faces yet. Scan your photos first (Library).'));
    for (const c of cl.clusters.slice(0, 30)) view.append(h('div', { class: 'card' }, h('div', { class: 'small muted' }, `Group of ${c.n} faces`), selectableFaces(c.faces, sel, upd)));
    if (cl.unclustered.length) view.append(h('div', { class: 'card' }, h('div', { class: 'small muted' }, 'Other large faces'), selectableFaces(cl.unclustered, sel, upd)));
    return;
  }
  const personCard = p => h('div', { class: 'card row', style: { cursor: 'pointer' }, onclick: () => { location.hash = `#/people/person/${p.id}`; } },
    h('div', { class: 'face', style: { width: '64px', flex: 'none' } }, p.cover ? faceImg(p.cover) : null),
    h('div', { class: 'grow' }, h('b', {}, p.is_me ? 'Me' : p.name), h('div', { class: 'small muted' }, `${p.n} faces · ${p.nref} references`)),
    p.nunc ? h('span', { class: 'chip on' }, `${p.nunc} to check`) : null);
  view.append(h('h2', {}, 'Me'), personCard(me));
  const others = pp.persons.filter(p => !p.is_me);
  view.append(h('h2', {}, 'People (only used as filters, never rated)'));
  if (!others.length) view.append(h('div', { class: 'muted' }, 'Open a group below and name it.'));
  others.forEach(p => view.append(personCard(p)));
  view.append(h('h2', {}, 'Unnamed groups'));
  const msel = new Set();
  const mergeBtn = h('button', { disabled: true, onclick: async () => { await api('/api/clusters/merge', { method: 'POST', body: { ids: [...msel] } }); toast('Merged'); route(); } }, 'Merge selected groups');
  view.append(h('div', { class: 'row', style: { marginBottom: '8px' } }, mergeBtn, h('button', { onclick: async () => { await api('/api/tasks/cluster', { method: 'POST' }); toast('Grouping again. This resets unnamed groups.'); } }, 'Group again')));
  const grid = h('div', { class: 'clusters' });
  for (const c of cl.clusters) {
    const chk = h('input', { type: 'checkbox', onclick: e => { e.stopPropagation(); chk.checked ? msel.add(c.id) : msel.delete(c.id); mergeBtn.disabled = msel.size < 2; } });
    grid.append(h('div', { class: 'card cluster', style: { cursor: 'pointer' }, onclick: () => { location.hash = `#/people/cluster/${c.id}`; } },
      h('div', { class: 'row' }, chk, h('span', { class: 'grow' }, `${c.n} faces`)), h('div', { class: 'faces' }, ...c.faces.slice(0, 8).map(id => h('div', { class: 'face' }, faceImg(id))))));
  }
  view.append(grid);
}

async function renderCluster(cid) {
  const [{ faces }, pp] = await Promise.all([api(`/api/clusters/${cid}`), api('/api/people')]);
  const sel = new Set(faces);
  view.append(h('div', { class: 'row' }, h('a', { href: '#/people' }, '← Me & People')), h('h1', {}, `Group ${cid}`),
    h('p', { class: 'muted' }, 'All faces are selected. Tap the faces that are NOT this person to unselect them.'));
  const name = h('input', { type: 'text', placeholder: 'Name, for example Dana' });
  const target = h('select', {}, h('option', { value: '' }, 'Add to a person…'), ...pp.persons.map(p => h('option', { value: p.id }, p.is_me ? 'Me' : p.name)));
  view.append(h('div', { class: 'card' },
    h('div', { class: 'row' }, name, h('button', { class: 'primary', onclick: async () => {
      if (!name.value.trim()) return toast('Type a name');
      await api('/api/persons', { method: 'POST', body: { name: name.value.trim(), face_ids: [...sel] } }); toast('Saved'); location.hash = '#/people';
    } }, 'Name selected faces')),
    h('div', { class: 'row', style: { marginTop: '8px' } }, target, h('button', { onclick: async () => {
      if (!target.value) return;
      await api(`/api/persons/${target.value}/faces`, { method: 'POST', body: { action: 'add', face_ids: [...sel] } }); toast('Added'); location.hash = '#/people';
    } }, 'Add selected'),
    h('button', { onclick: async () => { const r = await api('/api/clusters/split', { method: 'POST', body: { face_ids: [...sel] } }); toast('Moved to a new group'); location.hash = `#/people/cluster/${r.cluster}`; } }, 'Split selected into a new group'))));
  view.append(selectableFaces(faces, sel));
}

async function renderPerson(pid) {
  const pp = await api('/api/people');
  const p = pp.persons.find(x => x.id === pid);
  if (!p) { view.append(h('div', {}, 'Not found')); return; }
  view.append(h('div', { class: 'row' }, h('a', { href: '#/people' }, '← Me & People')));
  const title = h('h1', {}, p.is_me ? 'Me' : p.name);
  view.append(title);
  if (!p.is_me) {
    view.append(h('div', { class: 'row', style: { marginBottom: '10px' } },
      h('button', { onclick: async () => { const n = prompt('New name', p.name); if (n) { await api(`/api/persons/${pid}`, { method: 'POST', body: { name: n } }); route(); } } }, 'Rename'),
      h('button', { class: 'danger', onclick: async () => { if (confirm(`Delete ${p.name}? Faces become unnamed.`)) { await api(`/api/persons/${pid}`, { method: 'DELETE' }); location.hash = '#/people'; } } }, 'Delete')));
  }
  const unc = await api(`/api/persons/${pid}/uncertain?limit=48`);
  const ucard = h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, `Is this ${p.is_me ? 'me' : p.name}? (${p.nunc})`),
    h('p', { class: 'small muted' }, 'Your answers improve the matching for all photos.'));
  if (!unc.faces.length) ucard.append(h('div', { class: 'muted' }, 'Nothing to check now.'));
  const ugrid = h('div', { class: 'faces' });
  for (const f of unc.faces) {
    const el = h('div', { class: 'face' }, faceImg(f.id));
    const answer = async action => { el.remove(); await api(`/api/persons/${pid}/faces`, { method: 'POST', body: { action, face_ids: [f.id] } }); };
    el.append(h('div', { class: 'acts' }, h('button', { class: 'yes', onclick: e => { e.stopPropagation(); answer('confirm'); } }, '✓'), h('button', { class: 'no', onclick: e => { e.stopPropagation(); answer('reject'); } }, '✕')));
    ugrid.append(el);
  }
  ucard.append(ugrid);
  if (unc.faces.length) ucard.append(h('button', { style: { marginTop: '8px' }, onclick: route }, 'Show more'));
  view.append(ucard);
  const all = await api(`/api/persons/${pid}/faces?limit=300`);
  const rsel = new Set();
  view.append(h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, `Faces (${p.n})`),
    h('p', { class: 'small muted' }, 'Select wrong faces and remove them.'),
    h('button', { class: 'danger', onclick: async () => { if (!rsel.size) return; await api(`/api/persons/${pid}/faces`, { method: 'POST', body: { action: 'reject', face_ids: [...rsel] } }); toast('Removed'); route(); } }, 'Not this person'),
    h('div', { style: { marginTop: '8px' } }, selectableFaces(all.faces.map(f => f.id), rsel))));
}

// ------------------------------------------------------------------ Calibrate
async function renderCalibrate() {
  const sub = location.hash.split('/')[2];
  if (sub === 'surprising') return renderSurprising();
  const head = h('div', { class: 'row' }, h('h1', { class: 'grow', style: { margin: 0 } }, 'Calibrate'), h('a', { href: '#/calibrate/surprising' }, 'Most surprising photos'));
  const q = h('div', { class: 'card small' });
  const box = h('div');
  view.append(head, q, box);
  let current = null;
  let history = [];
  const showModel = m => {
    if (!m) return;
    q.innerHTML = '';
    q.append(h('b', {}, m.message), h('div', { class: 'muted' }, `${m.ratings} ratings, ${m.pairs} comparisons` + (m.spearman !== null && m.spearman !== undefined ? `, Spearman ${m.spearman.toFixed(2)} on held-out ratings` : '')));
  };

  const next = async (forceItem) => {
    const r = forceItem || await api('/api/calib/next');
    showModel(r.model);
    current = r;
    box.innerHTML = '';
    if (r.kind === 'done') { box.append(h('div', { class: 'card' }, 'No more photos to rate now. Scan more photos or check "Me & People".')); return; }
    if (r.kind === 'rate') drawRate(r.item); else drawPair(r.a, r.b);
  };

  const send = async (path, body) => { try { const r = await api(path, { method: 'POST', body }); showModel(r.model); } catch (e) { toast(e.message); } };
  const rate = async n => { if (current?.kind !== 'rate') return; history.push(current); await send('/api/calib/rate', { face_id: current.item.face_id, rating: n }); next(); };
  const skip = async () => { if (current?.kind !== 'rate') return; await send('/api/calib/skip', { face_id: current.item.face_id }); next(); };
  const undo = async () => { const r = await api('/api/calib/undo', { method: 'POST' }); if (r.ok && r.redo) { toast('Undone'); next({ ...r.redo, model: null }); } else toast('Nothing to undo'); };
  const compare = async res => { if (current?.kind !== 'pair') return; await send('/api/calib/compare', { a: current.a.face_id, b: current.b.face_id, result: res }); next(); };

  function fullWithBox(it) {
    const wrap = h('div', { class: 'full' });
    const img = h('img', { src: `/api/photo/${it.photo_id}/view`, alt: '' });
    const box = h('div', { class: 'facebox' });
    img.onload = () => {
      const r = img.getBoundingClientRect(), w = wrap.getBoundingClientRect();
      Object.assign(box.style, { left: (r.left - w.left + it.box[0] * r.width) + 'px', top: (r.top - w.top + it.box[1] * r.height) + 'px', width: ((it.box[2] - it.box[0]) * r.width) + 'px', height: ((it.box[3] - it.box[1]) * r.height) + 'px' });
    };
    wrap.append(img, box);
    return wrap;
  }

  function drawRate(it) {
    box.append(h('div', { class: 'cal' }, fullWithBox(it), h('div', { class: 'crop' }, h('img', { src: `/api/face/${it.face_id}/large`, alt: 'my face' }))));
    if (it.score !== null && it.score !== undefined) box.append(h('div', { class: 'small muted' }, `Current estimate: ${(it.prior ? '~' : '') + it.score.toFixed(1)}`));
    box.append(h('div', { class: 'cal-acts' },
      h('div', { class: 'small muted', style: { marginBottom: '6px' } }, 'How good do I look in this photo? ', h('span', { class: 'kbd' }, '1'), '…', h('span', { class: 'kbd' }, '9'), ' ', h('span', { class: 'kbd' }, '0'), '=10 · ', h('span', { class: 'kbd' }, 'S'), ' skip · ', h('span', { class: 'kbd' }, 'U'), ' undo'),
      h('div', { class: 'rate10' }, ...Array.from({ length: 10 }, (_, k) => h('button', { onclick: () => rate(k + 1) }, String(k + 1)))),
      h('div', { class: 'row', style: { marginTop: '8px' } }, h('button', { onclick: skip }, 'Skip'), h('button', { onclick: undo }, 'Undo'))));
  }

  function drawPair(a, b) {
    const side = (it, res) => h('div', { class: 'side', onclick: () => compare(res) },
      h('img', { src: `/api/face/${it.face_id}/large`, alt: '' }), h('img', { class: 'smallfull', src: `/api/photo/${it.photo_id}/view`, alt: '' }));
    const pr = h('div', { class: 'pair' }, side(a, 'a'), side(b, 'b'));
    onSwipe(pr, dir => compare(dir === 'left' ? 'a' : 'b'));
    box.append(h('h2', {}, 'Which photo of me is better?'), pr,
      h('div', { class: 'cal-acts' }, h('div', { class: 'small muted', style: { marginBottom: '6px' } }, 'Tap or swipe toward the better one. ', h('span', { class: 'kbd' }, '←'), ' left · ', h('span', { class: 'kbd' }, '→'), ' right · ', h('span', { class: 'kbd' }, '↓'), ' equal'),
        h('div', { class: 'row' }, h('button', { class: 'grow', onclick: () => compare('a') }, '← Left'), h('button', { class: 'grow', onclick: () => compare('eq') }, 'Equal'), h('button', { class: 'grow', onclick: () => compare('b') }, 'Right →')),
        h('div', { class: 'row', style: { marginTop: '8px' } }, h('button', { onclick: () => api('/api/calib/next?skip_pair=true').then(r => next(r)) }, 'Skip'), h('button', { onclick: undo }, 'Undo'))));
  }

  keyHandler = e => {
    if (e.target.tagName === 'INPUT') return;
    if (current?.kind === 'rate') {
      if (/^[1-9]$/.test(e.key)) rate(+e.key); else if (e.key === '0') rate(10);
      else if (e.key.toLowerCase() === 's') skip(); else if (e.key.toLowerCase() === 'u') undo();
    } else if (current?.kind === 'pair') {
      if (e.key === 'ArrowLeft') compare('a'); else if (e.key === 'ArrowRight') compare('b'); else if (e.key === 'ArrowDown') compare('eq');
      else if (e.key.toLowerCase() === 'u') undo();
    }
  };
  document.addEventListener('keydown', keyHandler);
  next();
}

async function renderSurprising() {
  view.append(h('div', { class: 'row' }, h('a', { href: '#/calibrate' }, '← Calibrate')), h('h1', {}, 'Most surprising photos'),
    h('p', { class: 'muted' }, 'The model and your rating disagree most on these photos. Tap one to correct your rating.'));
  const r = await api('/api/calib/surprising');
  if (!r.items.length) { view.append(h('div', { class: 'card' }, 'Train a model first (rate at least 10 photos).')); return; }
  const grid = h('div', { class: 'grid' });
  for (const d of r.items) {
    const t = h('div', { class: 'tile' }, h('img', { src: `/api/face/${d.face_id}/large`, loading: 'lazy' }),
      h('span', { class: 'badge ' + scoreClass(d.rating) }, `me ${d.rating} · model ${d.predicted}`));
    t.onclick = () => {
      const n = prompt(`New rating for this photo (1-10). Now: ${d.rating}`, d.rating);
      if (n && +n >= 1 && +n <= 10) api('/api/calib/rate', { method: 'POST', body: { face_id: d.face_id, rating: +n, source: 'correction' } }).then(() => { toast('Saved'); t.querySelector('.badge').textContent = `me ${n} · model ${d.predicted}`; });
    };
    grid.append(t);
  }
  view.append(grid);
}

// ------------------------------------------------------------------ Gallery
async function renderGallery() {
  const s = await api('/api/settings');
  const root = h('div');
  view.append(root);
  createGallery(root, pcProvider(s));
}

// ------------------------------------------------------------------ Settings
async function renderSettings() {
  const st = await loadStatus();
  const s = st.settings;
  view.append(h('h1', {}, 'Settings'));
  const local = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
  const num = (key, label, min, max, step = 1) => {
    const i = h('input', { type: 'number', min, max, step, value: s[key], style: { width: '110px' } });
    return h('div', { class: 'row' }, h('span', { class: 'grow' }, label), i, h('button', { onclick: async () => { await api('/api/settings', { method: 'POST', body: { [key]: +i.value } }); toast('Saved'); } }, 'Save'));
  };
  const tog = (key, label, note) => h('div', {}, h('label', { class: 'chip' + (s[key] ? ' on' : ''), onclick: async () => { await api('/api/settings', { method: 'POST', body: { [key]: !s[key] } }); route(); } }, label), note ? h('div', { class: 'small muted' }, note) : null);
  view.append(h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, 'Processing'),
    num('workers', 'CPU worker processes (default 5 on a 6-core CPU)', 1, 32),
    tog('low_priority', 'Low priority mode', 'Half the workers at idle priority, and pauses for the GPU. Use it while you use the PC.'),
    num('preview_cache_mb', 'Preview cache limit (MB)', 50, 100000, 50),
    num('retrain_every', 'Retrain after this many new ratings', 1, 100)));
  view.append(h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, 'Face matching'),
    num('match_accept', 'Accept a match at similarity', 0.2, 0.9, 0.01),
    num('match_reject', 'Ignore below similarity (between: you check)', 0.1, 0.8, 0.01)));
  view.append(h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, 'Map'),
    tog('map_enabled', 'Show the map view', 'Map tiles come from openstreetmap.org. This sends the map areas you look at to that server. Turn it off for fully offline use. All filters work without the map.')));

  const lan = h('div', { class: 'card' }, h('h2', { style: { marginTop: 0 } }, 'Phone and LAN access'));
  if (!local) lan.append(h('p', { class: 'muted' }, 'You can change network settings only on the PC.'));
  else {
    const pw = h('input', { type: 'password', placeholder: s.lan_password_set ? 'New password (optional)' : 'Password (6+ characters)', autocomplete: 'new-password' });
    lan.append(h('p', { class: 'small muted' }, 'Off: only this PC can open the app. On: devices on your home network can open it with the password, and the phone app can sync. Traffic on the LAN is not encrypted (plain HTTP).'),
      h('div', { class: 'row' }, pw,
        h('button', { class: s.lan_enabled ? '' : 'primary', onclick: async () => {
          try { const r = await api('/api/settings/lan', { method: 'POST', body: { enabled: !s.lan_enabled, password: pw.value || null } }); toast(r.restarting ? 'Restarting the server…' : 'Saved'); setTimeout(() => location.reload(), 3000); } catch (e) { toast(e.message); }
        } }, s.lan_enabled ? 'Turn LAN access off' : 'Turn LAN access on'),
        s.lan_enabled ? h('button', { onclick: async () => { if (!pw.value) return toast('Type the new password'); await api('/api/settings/lan', { method: 'POST', body: { enabled: true, password: pw.value } }); toast('Password changed'); } }, 'Change password') : null));
    if (s.lan_enabled && st.lan.url) {
      lan.append(h('h3', {}, 'Open on the phone browser'), h('div', { class: 'row' }, h('img', { class: 'qr', src: `/api/lan/qr.svg?text=${encodeURIComponent(st.lan.url)}`, alt: 'QR code' }),
        h('div', {}, h('b', {}, st.lan.url), h('div', { class: 'small muted' }, 'Other addresses: ' + st.lan.addresses.map(a => `${a.ip} (${a.iface})`).join(', ')))));
      const pairBox = h('div');
      lan.append(h('h3', {}, 'Pair the phone app'), h('button', { class: 'primary', onclick: async () => {
        const r = await api('/api/pair/start', { method: 'POST' });
        pairBox.innerHTML = '';
        pairBox.append(h('div', { class: 'row' }, h('img', { class: 'qr', src: `/api/lan/qr.svg?text=${encodeURIComponent(r.link)}`, alt: 'Pairing QR code' }),
          h('div', {}, h('div', {}, 'Scan with the phone camera, or type in the app:'), h('div', { class: 'kv', style: { marginTop: '6px' } },
            h('span', {}, 'Address'), h('b', {}, r.host), h('span', {}, 'Port'), h('b', {}, r.port), h('span', {}, 'Code'), h('b', { style: { fontSize: '22px', letterSpacing: '3px' } }, r.code)),
          h('div', { class: 'small muted' }, 'The code is valid for 10 minutes, for one phone.'))));
      } }, 'Pair phone'), pairBox);
    }
    if (st.devices.length) {
      lan.append(h('h3', {}, 'Paired phones'));
      for (const d of st.devices) lan.append(h('div', { class: 'row' }, h('span', { class: 'grow' }, `${d.name} (${d.connected ? 'connected' : 'not connected'})`),
        h('button', { class: 'danger', onclick: async () => { if (confirm('Unpair this phone? Its photos stay on the PC.')) { await api(`/api/devices/${d.id}`, { method: 'DELETE' }); route(); } } }, 'Unpair')));
    }
  }
  view.append(lan);
  view.append(h('div', { class: 'card small muted' }, `Rated Gallery ${st.version}. Everything runs on this PC. No cloud, no telemetry.`));
}

route();
