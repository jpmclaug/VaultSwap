// VaultSwap Progressive Web App Service Worker
const CACHE_VERSION = 'vaultswap-v1';
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const RUNTIME_CACHE = `${CACHE_VERSION}-runtime`;

const PRECACHE_ASSETS = [
  '/',
  '/offline',
  '/manifest.webmanifest',
  '/static/manifest.webmanifest',
  '/static/img/icons/icon-192.png',
  '/static/img/icons/icon-512.png',
  '/static/img/icons/apple-touch-icon.png',
  '/static/img/icons/favicon-32x32.png',
  '/static/img/vaultswap-logo.jpg',
  '/static/js/binder_controls.js'
];

// Install: precache critical shell and offline page
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(async (cache) => {
      // Use map + catch so individual item failures don't abort entire service worker registration
      await Promise.all(
        PRECACHE_ASSETS.map((url) =>
          cache.add(url).catch((err) => {
            console.warn(`[SW] Precache skipped for ${url}:`, err);
          })
        )
      );
      return self.skipWaiting();
    })
  );
});

// Activate: clean up outdated caches and claim clients immediately
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys
          .filter((key) => key.startsWith('vaultswap-') && key !== STATIC_CACHE && key !== RUNTIME_CACHE)
          .map((key) => {
            console.log(`[SW] Purging outdated cache: ${key}`);
            return caches.delete(key);
          })
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch: Strategy depending on request type
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // 1. Only intercept HTTP/HTTPS GET requests
  if (req.method !== 'GET' || !url.protocol.startsWith('http')) {
    return;
  }

  // 2. Skip authentication and dynamic mutation endpoints to guarantee freshness
  const bypassPrefixes = ['/auth', '/login/dev', '/logout', '/api/', '/mcp'];
  if (bypassPrefixes.some((p) => url.pathname.startsWith(p))) {
    return;
  }

  // 3. Navigation requests (HTML pages): Network-first with dynamic caching and offline fallback
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(RUNTIME_CACHE).then((cache) => cache.put(req, copy));
          }
          return response;
        })
        .catch(async () => {
          const cached = await caches.match(req);
          if (cached) return cached;
          const offlineFallback = await caches.match('/offline');
          if (offlineFallback) return offlineFallback;
          return new Response(
            '<!DOCTYPE html><html><head><meta charset="utf-8"><title>VaultSwap // Offline</title></head><body style="background:#020617;color:#f8fafc;font-family:sans-serif;text-align:center;padding:40px 16px;"><h1>You are offline</h1><p>Please check your connection and retry.</p></body></html>',
            { headers: { 'Content-Type': 'text/html' } }
          );
        })
    );
    return;
  }

  // 4. Static assets (images, CSS, JS, fonts): Stale-While-Revalidate
  const isStatic =
    url.pathname.startsWith('/static/') ||
    url.hostname.includes('tailwindcss.com') ||
    url.hostname.includes('htmx.org') ||
    url.hostname.includes('scryfall.io');

  if (isStatic) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const fetchPromise = fetch(req)
          .then((networkResponse) => {
            if (networkResponse && networkResponse.status === 200) {
              const copy = networkResponse.clone();
              caches.open(STATIC_CACHE).then((cache) => cache.put(req, copy));
            }
            return networkResponse;
          })
          .catch(() => cached);

        return cached || fetchPromise;
      })
    );
    return;
  }

  // 5. Default network-first with cache fallback
  event.respondWith(
    fetch(req).catch(() => caches.match(req))
  );
});

// Listener for skipWaiting message from client
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
