// Small shared helpers (PC UI and phone gallery).
export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.body && typeof o.body !== 'string') { o.body = JSON.stringify(o.body); o.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(path, o);
  if (r.status === 401) { location.href = '/login'; throw new Error('login required'); }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

export function fmtEta(s) {
  if (s === null || s === undefined) return '';
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}

export function toast(msg, ms = 2500) {
  let t = document.getElementById('toast');
  if (!t) { t = h('div', { id: 'toast' }); document.body.append(t); }
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._tm); t._tm = setTimeout(() => t.classList.remove('show'), ms);
}

export function scoreClass(s) {
  if (s === null || s === undefined) return 'none';
  return s >= 8 ? 'hi' : s >= 5 ? 'mid' : 'lo';
}

export const isTouch = () => matchMedia('(pointer: coarse)').matches;

// Pinch zoom and pan for one image inside a container. Double tap / double click toggles 2.5x.
export function zoomable(container, img) {
  let s = 1, x = 0, y = 0, start = null;
  const pts = new Map();
  const apply = () => { img.style.transform = `translate(${x}px, ${y}px) scale(${s})`; };
  const clamp = () => { if (s <= 1) { s = 1; x = 0; y = 0; } };
  container.style.touchAction = 'none';
  container.addEventListener('pointerdown', e => {
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    container.setPointerCapture(e.pointerId);
    start = { s, x, y, pts: new Map([...pts].map(([k, v]) => [k, { ...v }])) };
  });
  container.addEventListener('pointermove', e => {
    if (!pts.has(e.pointerId) || !start) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const cur = [...pts.values()], st = [...start.pts.values()];
    if (cur.length >= 2 && st.length >= 2) {
      const d0 = Math.hypot(st[0].x - st[1].x, st[0].y - st[1].y), d1 = Math.hypot(cur[0].x - cur[1].x, cur[0].y - cur[1].y);
      s = Math.min(8, Math.max(1, start.s * d1 / Math.max(d0, 1)));
      x = start.x + ((cur[0].x + cur[1].x) - (st[0].x + st[1].x)) / 2;
      y = start.y + ((cur[0].y + cur[1].y) - (st[0].y + st[1].y)) / 2;
    } else if (s > 1 && cur.length === 1) {
      x = start.x + cur[0].x - st[0].x; y = start.y + cur[0].y - st[0].y;
    }
    clamp(); apply();
  });
  const up = e => { pts.delete(e.pointerId); start = pts.size ? { s, x, y, pts: new Map([...pts].map(([k, v]) => [k, { ...v }])) } : null; };
  container.addEventListener('pointerup', up);
  container.addEventListener('pointercancel', up);
  container.addEventListener('dblclick', () => { if (s > 1) { s = 1; x = 0; y = 0; } else s = 2.5; apply(); });
  container.addEventListener('wheel', e => { e.preventDefault(); s = Math.min(8, Math.max(1, s * (e.deltaY < 0 ? 1.15 : 0.87))); clamp(); apply(); }, { passive: false });
  return { reset() { s = 1; x = 0; y = 0; apply(); }, get zoomed() { return s > 1.01; } };
}

// Horizontal swipe detection (only when not zoomed).
export function onSwipe(el, cb, isBlocked = () => false) {
  let sx = null, sy = null, n = 0;
  el.addEventListener('touchstart', e => { n = e.touches.length; if (n === 1) { sx = e.touches[0].clientX; sy = e.touches[0].clientY; } }, { passive: true });
  el.addEventListener('touchend', e => {
    if (sx === null || n !== 1 || isBlocked()) { sx = null; return; }
    const dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) cb(dx < 0 ? 'left' : 'right');
    sx = null;
  }, { passive: true });
}
