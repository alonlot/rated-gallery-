// Pure functions (no DOM): bursts, trips and events. Used by the gallery on the PC and in the phone app,
// and tested with Node (tests/groups_check.mjs).

export function km(lat1, lon1, lat2, lon2) {
  const p = Math.PI / 180;
  const a = Math.sin((lat2 - lat1) * p / 2) ** 2 + Math.cos(lat1 * p) * Math.cos(lat2 * p) * Math.sin((lon2 - lon1) * p / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(Math.min(1, a)));
}

const val = (p, key) => (p[key] === null || p[key] === undefined ? -1 : p[key]);

// One photo per burst: the best one (highest score), placed where the burst first appears in `list`.
// Returns { items, others } where others.get(burstId) = the other photos of that burst (best first).
export function collapseBursts(list, scoreKey = 's') {
  const members = new Map();
  for (const p of list) if (p.b !== null && p.b !== undefined) {
    if (!members.has(p.b)) members.set(p.b, []);
    members.get(p.b).push(p);
  }
  const items = [], others = new Map(), done = new Set();
  for (const p of list) {
    if (p.b === null || p.b === undefined || members.get(p.b).length < 2) { items.push(p); continue; }
    if (done.has(p.b)) continue;
    done.add(p.b);
    const ms = [...members.get(p.b)].sort((a, b) => val(b, scoreKey) - val(a, scoreKey));
    items.push(ms[0]);
    others.set(p.b, ms.slice(1));
  }
  return { items, others };
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const ts = t => Date.parse(t.length === 19 ? t + 'Z' : t);  // local time as written; treat as UTC for arithmetic

export function dateRange(t1, t2) {
  const a = new Date(ts(t1)), b = new Date(ts(t2));
  const d = x => x.getUTCDate(), m = x => MONTHS[x.getUTCMonth()], y = x => x.getUTCFullYear();
  if (a.toISOString().slice(0, 10) === b.toISOString().slice(0, 10)) return `${DAYS[a.getUTCDay()]} ${d(a)} ${m(a)} ${y(a)}`;
  if (y(a) === y(b) && m(a) === m(b)) return `${d(a)}–${d(b)} ${m(a)} ${y(a)}`;
  if (y(a) === y(b)) return `${d(a)} ${m(a)} – ${d(b)} ${m(b)} ${y(a)}`;
  return `${d(a)} ${m(a)} ${y(a)} – ${d(b)} ${m(b)} ${y(b)}`;
}

export function monthLabel(t) {
  if (!t) return 'No date';
  return `${MONTHS[+t.slice(5, 7) - 1]} ${t.slice(0, 4)}`;
}

function mostCommon(values) {
  const m = new Map();
  for (const v of values) if (v) m.set(v, (m.get(v) || 0) + 1);
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
}

function summarize(kind, photos, title, scoreKey) {
  const t = photos.map(p => p.t).sort();
  const me = photos.filter(p => p.me);
  const best = [...photos].sort((a, b) => (b.me - a.me) || (val(b, scoreKey) - val(a, scoreKey)));
  const days = Math.max(1, Math.round((ts(t[t.length - 1]) - ts(t[0])) / 86400000) + 1);
  return { kind, title, when: dateRange(t[0], t[t.length - 1]), start: t[0], end: t[t.length - 1], days,
    ids: photos.map(p => p.id), count: photos.length, me: me.length,
    best: best.slice(0, 4).map(p => p.id), bestScore: me.length ? Math.max(...me.map(p => val(p, scoreKey))) : null };
}

// Home = the city with the most located photos (mean position of its photos).
export function findHome(photos, minLocated = 10) {
  const located = photos.filter(p => p.lat !== null && p.lat !== undefined);
  if (located.length < minLocated) return null;
  const top = mostCommon(located.map(p => p.city && `${p.city}|${p.cc}`))[0];
  if (!top) return null;
  const [city, cc] = top[0].split('|');
  const hs = located.filter(p => p.city === city && p.cc === cc);
  return { city, cc, lat: hs.reduce((s, p) => s + p.lat, 0) / hs.length, lon: hs.reduce((s, p) => s + p.lon, 0) / hs.length };
}

// Trips: runs of photos more than `awayKm` from home, with gaps of at most `maxGapDays`.
// Events: photos with gaps of at most `eventGapH` hours, at least `minEvent` photos, mostly outside trips.
export function findTrips(photos, { awayKm = 80, maxGapDays = 2, minTrip = 5, eventGapH = 3, minEvent = 6, scoreKey = 's' } = {}) {
  const dated = photos.filter(p => p.t).sort((a, b) => a.t.localeCompare(b.t));
  const home = findHome(photos);
  const trips = [];
  const inTrip = new Set();
  if (home) {
    const away = p => p.lat !== null && p.lat !== undefined && km(home.lat, home.lon, p.lat, p.lon) > awayKm;
    const atHome = p => p.lat !== null && p.lat !== undefined && !away(p);
    let cur = null, lastAway = null;
    const close = () => {
      if (cur) {
        const keep = cur.slice(0, cur.lastIndexOf(lastAway) + 1);  // drop unlocated photos after the last away photo
        if (keep.length >= minTrip) {
          const aw = keep.filter(away);
          const countries = mostCommon(aw.map(p => p.country));
          let place;
          if (countries.length && aw[0] && countries[0][0] && aw.every(p => !p.cc || p.cc === home.cc)) place = (mostCommon(aw.map(p => p.city))[0] || ['Trip'])[0];
          else if (countries.length > 1 && countries[1][1] >= 0.3 * aw.length) place = `${countries[0][0]} and ${countries[1][0]}`;
          else place = (countries[0] || ['Trip'])[0];
          trips.push(summarize('trip', keep, place, scoreKey));
          keep.forEach(p => inTrip.add(p.id));
        }
      }
      cur = null; lastAway = null;
    };
    for (const p of dated) {
      const gapOk = lastAway && (ts(p.t) - ts(lastAway.t)) <= maxGapDays * 86400000;
      if (away(p)) {
        if (cur && !gapOk) close();
        if (!cur) cur = [];
        cur.push(p); lastAway = p;
      } else if (cur) {
        if (atHome(p) || !gapOk) close();
        else cur.push(p);
      }
    }
    close();
  }
  const events = [];
  let grp = [];
  const flush = () => {
    if (grp.length >= minEvent && grp.filter(p => inTrip.has(p.id)).length < 0.8 * grp.length) {
      const city = (mostCommon(grp.map(p => p.city))[0] || [null])[0];
      events.push(summarize('event', grp, city || 'Event', scoreKey));
    }
    grp = [];
  };
  for (const p of dated) {
    if (grp.length && ts(p.t) - ts(grp[grp.length - 1].t) > eventGapH * 3600000) flush();
    grp.push(p);
  }
  flush();
  const newest = (a, b) => b.start.localeCompare(a.start);
  return { home, trips: trips.sort(newest), events: events.sort(newest) };
}
