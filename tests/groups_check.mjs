// Checks ratedgallery/static/groups.js: bursts, home, trips, events (synthetic data).
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
const here = path.dirname(fileURLToPath(import.meta.url));
const G = await import(pathToFileURL(path.join(here, '..', 'ratedgallery', 'static', 'groups.js')).href);
let fails = 0;
const ok = (c, m) => { if (!c) { console.log('FAIL', m); fails++; } };
let id = 0;
const P = (t, lat, lon, city, cc, country, extra = {}) => ({ id: ++id, t, lat, lon, city, cc, country, me: true, s: 5, b: null, ...extra });
const TA = [32.08, 34.78, 'Tel Aviv', 'IL', 'Israel'], ROME = [41.9, 12.49, 'Rome', 'IT', 'Italy'], FLO = [43.77, 11.25, 'Florence', 'IT', 'Italy'];
const EIL = [29.55, 34.95, 'Eilat', 'IL', 'Israel'];
const photos = [];
for (let d = 1; d <= 28; d++) photos.push(P(`2024-03-${String(d).padStart(2, '0')}T10:00:00`, ...TA));  // home, many days
// trip to Italy 2-6 May, one photo without GPS in the middle
['2024-05-02T09:00:00', '2024-05-02T15:00:00', '2024-05-03T11:00:00'].forEach(t => photos.push(P(t, ...ROME, { s: 7 })));
photos.push(P('2024-05-04T12:00:00', null, null, null, null, null, { s: 9.5 }));
['2024-05-05T10:00:00', '2024-05-06T18:00:00'].forEach(t => photos.push(P(t, ...FLO)));
photos.push(P('2024-05-07T20:00:00', ...TA));                         // back home
// domestic trip to Eilat
['2024-08-10T09:00:00', '2024-08-10T13:00:00', '2024-08-11T09:00:00', '2024-08-11T17:00:00', '2024-08-12T08:00:00'].forEach(t => photos.push(P(t, ...EIL)));
// an event at home: 8 photos in 2 hours
for (let k = 0; k < 8; k++) photos.push(P(`2024-09-15T19:${String(k * 7).padStart(2, "0")}:00`, ...TA, { me: k % 2 === 0, s: k % 2 === 0 ? 6 + k / 2 : null }));
// photos without date are ignored
photos.push(P(null, ...ROME));

const r = G.findTrips(photos);
ok(r.home && r.home.city === 'Tel Aviv', 'home is Tel Aviv');
const italy = r.trips.find(t => t.title === 'Italy');
ok(italy && italy.count === 6, 'Italy trip has 6 photos (with the photo without GPS) ' + JSON.stringify(italy && italy.count));
ok(italy && italy.when === '2–6 May 2024' && italy.days === 5, 'Italy dates ' + (italy && italy.when));
ok(italy && italy.bestScore === 9.5 && italy.best[0] === photos.find(p => p.s === 9.5).id, 'best photo of the trip first');
const eilat = r.trips.find(t => t.title === 'Eilat');
ok(eilat && eilat.count === 5, 'domestic trip named by city');
ok(r.trips.length === 2, 'two trips, home days are not trips');
ok(r.trips[0].title === 'Eilat', 'newest trip first');
const party = r.events.find(e => e.title === 'Tel Aviv' && e.count === 8);
ok(party && party.me === 4 && party.when.startsWith('Sun 15 Sep 2024'), 'home event ' + JSON.stringify(party));
ok(!r.events.some(e => e.ids.every(i => italy.ids.includes(i))), 'trip photos are not repeated as an event');
ok(G.findTrips(photos.slice(0, 5)).home === null, 'no home with few located photos');

// bursts
const L = [{ id: 1, b: 7, s: 5 }, { id: 2, b: null, s: 9 }, { id: 3, b: 7, s: 8 }, { id: 4, b: 7, s: null }, { id: 5, b: 9, s: 4 }];
const c = G.collapseBursts(L);
ok(JSON.stringify(c.items.map(p => p.id)) === '[3,2,5]', 'best of burst at the first position ' + JSON.stringify(c.items.map(p => p.id)));
ok(JSON.stringify(c.others.get(7).map(p => p.id)) === '[1,4]', 'other burst photos, best first');
ok(!c.others.has(9), 'a single photo is not a burst');
ok(G.collapseBursts(L, 'sp').items.length === 3, 'works with the per-period score key');
ok(G.dateRange('2024-12-30T10:00:00', '2025-01-02T10:00:00') === '30 Dec 2024 – 2 Jan 2025', 'year change');
ok(G.monthLabel('2024-05-02T09:00:00') === 'May 2024' && G.monthLabel(null) === 'No date', 'month label');
console.log(fails ? `${fails} failed` : 'all group checks passed');
process.exit(fails ? 1 : 0);
