/* QUWWAA service worker — installable home-screen app shell.
   Caches the static console so it opens instantly and works offline; never
   caches live data (news, butler, transcription) so those always hit network. */
const CACHE = 'quwwaa-v1';
const SHELL = ['/', '/quwwaa-console.html', '/manifest.json',
               '/icon-192.png', '/icon-512.png', '/icon-180.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
                 .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // /ask, /transcribe stay on network
  const url = new URL(req.url);
  if (url.pathname.startsWith('/news') || url.pathname.startsWith('/ask') ||
      url.pathname.startsWith('/transcribe') || url.pathname === '/health') return;
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res && res.ok && url.origin === location.origin) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match('/quwwaa-console.html')))
  );
});
