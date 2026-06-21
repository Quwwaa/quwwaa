/* QUWWAA service worker — installable home-screen app shell.
   Strategy:
     - live data (news, butler, transcription, health) -> always network, never cached
     - the SW script itself -> never intercepted (so updates always flow)
     - static assets (icons, manifest) -> cache-first (fast, offline-friendly)
     - the page / HTML -> network-first, falling back to cache only when offline
   Bump CACHE on any shell change to retire old caches. */
const CACHE = 'quwwaa-v58';
const SHELL = ['/', '/quwwaa-console.html', '/manifest.json',
               '/icon-192.png', '/icon-512.png', '/icon-180.png', '/logo-q.png'];

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
  if (req.method !== 'GET') return;                         // /ask, /transcribe stay on network
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;               // let cross-origin (images, etc.) pass through
  // Home snapshot: paint instantly from cache, refresh in the background.
  if (url.pathname.startsWith('/home')) {
    e.respondWith(caches.open(CACHE).then(c => c.match(req).then(hit => {
      const net = fetch(req).then(res => { if (res && res.ok) c.put(req, res.clone()); return res; }).catch(() => hit);
      return hit || net;
    })));
    return;
  }
  if (/^\/(news|ask|transcribe|speak|health|service-worker\.js)/.test(url.pathname)) return;

  const isAsset = /\.(png|json|ico|svg|webmanifest|css)$/.test(url.pathname);
  if (isAsset) {
    e.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); return res;
      }))
    );
  } else {
    // HTML / navigations: network-first so the latest console always wins online
    e.respondWith(
      fetch(req).then(res => {
        const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); return res;
      }).catch(() => caches.match(req).then(hit => hit || caches.match('/quwwaa-console.html')))
    );
  }
});

/* ---- Web Push: show the notification, and deep-link on click ---- */
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data && e.data.text() }; }
  const title = d.title || 'QUWWAA';
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || '',
    icon: d.icon || '/icon-192.png',
    badge: '/icon-192.png',
    data: { url: d.url || '/' }
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(ws => {
    for (const w of ws) {
      if ('focus' in w) { if (w.navigate) { try { w.navigate(url); } catch (_) {} } return w.focus(); }
    }
    return clients.openWindow(url);
  }));
});
