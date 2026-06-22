/* QUWWAA HQ (Jarvis cockpit) service worker — installable shell only.
   The cockpit is live-data and admin-gated, so we never cache API responses
   (/jarvis/stats, /config, /speak, /ask, auth) — only the app shell + icons, so
   it opens from the home screen and the numbers are always fresh from network. */
const CACHE = 'quwwaa-hq-v26';
const SHELL = ['/jarvis.html', '/jarvis-manifest.json', '/icon-192.png', '/icon-512.png', '/icon-180.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE && k.startsWith('quwwaa-hq')).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                         // never touch POSTs (/speak, /ask, auth)
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;               // let cross-origin (supabase, fonts) pass through
  // Live, admin-gated data is always network — never served from cache.
  if (/^\/(jarvis\/stats|config|ask|speak|transcribe|auth)/.test(url.pathname)) return;
  const isShell = url.pathname === '/' || /\.(html|json|png|ico|svg)$/.test(url.pathname);
  if (!isShell) return;
  // Shell: network-first so an updated cockpit always wins online, cache as offline fallback.
  e.respondWith(
    fetch(req).then(res => { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); return res; })
              .catch(() => caches.match(req).then(hit => hit || caches.match('/jarvis.html')))
  );
});
