// Genius — Service Worker
// Makes the site installable and lets the page shell open offline.
// API calls (a different origin) are never cached: store data is always live.

const CACHE_NAME = "genius-cache-v2";
// relative to this file, so it works under /genius---platform/ on GitHub Pages
const ASSETS_TO_CACHE = ["./", "index.html", "manifest.json", "js/api.js",
                         "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS_TO_CACHE)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  // only same-origin GETs; the backend API and CDNs go straight to the network
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  // network first, so a new version of the page shows up right away
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match("index.html")))
  );
});
