// Checks the gallery filter logic (ratedgallery/static/gallery.js) on a real index from stdin.
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const { applyFilters } = await import(pathToFileURL(path.join(here, '..', 'ratedgallery', 'static', 'gallery.js')).href);
const idx = JSON.parse(readFileSync(0, 'utf8'));
const P = idx.photos;
const me = idx.persons.find(p => p.me).id;
const dana = (idx.persons.find(p => !p.me) || {}).id;
const base = { minScore: 0, hideLowConf: false, sort: 'score', includeNoMe: false, peopleMode: 'any', people: [], match: 'all',
  dateFrom: '', dateTo: '', year: '', month: '', tod: '', noDate: 'any', country: '', city: '', rLat: '', rLon: '', rKm: '',
  noLoc: 'any', camera: '', facing: '', npMin: '', npMax: '', src: '' };
const F = o => applyFilters(P, { ...base, ...o }, me);
let fails = 0;
const ok = (c, m) => { if (!c) { console.log('FAIL', m); fails++; } };

ok(F({}).length === P.filter(p => p.me).length, 'default shows photos with me');
ok(F({ includeNoMe: true }).length === P.length, 'all photos');
ok(F({ minScore: 8 }).every(p => p.s >= 8), 'min score');
ok(F({ hideLowConf: true }).every(p => p.c >= 0.4), 'hide low confidence');
ok(F({ peopleMode: 'alone' }).every(p => p.me && p.np <= 1), 'me alone');
if (dana) {
  const w = F({ peopleMode: 'with', people: [dana], match: 'all' });
  ok(w.length > 0 && w.every(p => p.p.includes(dana) && p.me), 'me with Dana');
  ok(F({ peopleMode: 'with', people: [dana], match: 'only' }).every(p => p.p.every(x => x === me || x === dana)), 'only me and Dana');
  ok(F({ includeNoMe: true, peopleMode: 'with', people: [dana], match: 'any' }).length >= w.length, 'any');
}
ok(F({ country: 'IT' }).every(p => p.cc === 'IT'), 'country');
const noLoc = F({ noLoc: 'only' });
ok(noLoc.length > 0 && noLoc.every(p => p.lat === null), 'no location');
ok(F({ noLoc: 'exclude' }).every(p => p.lat !== null), 'exclude no location');
ok(F({ year: '2024' }).every(p => p.t.startsWith('2024')), 'year');
ok(F({ month: '3' }).every(p => p.t.slice(5, 7) === '03'), 'month');
ok(F({ tod: 'evening' }).every(p => { const h = +p.t.slice(11, 13); return h >= 17 && h < 21; }), 'time of day');
const paris = F({ rLat: '48.8566', rLon: '2.3522', rKm: '50' });
ok(paris.length > 0 && paris.every(p => p.city === 'Paris'), 'radius');
ok(F({ facing: 'front' }).every(p => p.facing === 'front'), 'front camera');
ok(F({ npMin: '2' }).every(p => p.np >= 2), 'people count');
const s = F({});
ok(s.every((p, i) => i === 0 || (s[i - 1].s ?? -1) >= (p.s ?? -1)), 'sorted by score');
const d = F({ sort: 'date' });
ok(d.every((p, i) => i === 0 || (d[i - 1].t || '') >= (p.t || '')), 'sorted by date');
const combo = F({ country: 'FR', year: '2024', minScore: 5, facing: 'rear' });
ok(combo.every(p => p.cc === 'FR' && p.t.startsWith('2024') && p.s >= 5 && p.facing === 'rear'), 'combined filters');
console.log(fails ? `${fails} failed` : 'all filter checks passed');
process.exit(fails ? 1 : 0);
