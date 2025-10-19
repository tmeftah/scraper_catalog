// /sw.js
/* eslint-disable no-restricted-globals */
const VERSION = "v1";
const STATIC_CACHE = `static-${VERSION}`;
const RUNTIME_CACHE = `runtime-${VERSION}`;

const APP_SHELL = [
  "/",
  "/offline.html",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Enable navigation preload if available
      if (self.registration.navigationPreload) {
        await self.registration.navigationPreload.enable();
      }
      // Clean old caches
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => ![STATIC_CACHE, RUNTIME_CACHE].includes(k))
          .map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Handle navigation requests (HTML pages)
  if (request.mode === "navigate") {
    event.respondWith(networkFirstWithOfflineFallback(event));
    return;
  }

  // Runtime caching for assets (scripts, styles, images, fonts)
  const dest = request.destination;
  if (["style", "script", "image", "font"].includes(dest)) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }

  // Default: try network then cache
  event.respondWith(fetch(request).catch(() => caches.match(request)));
});

async function networkFirstWithOfflineFallback(event) {
  try {
    // Prefer preload (Chrome), then network
    const preload = await event.preloadResponse;
    if (preload) return preload;

    const network = await fetch(event.request);
    const cache = await caches.open(RUNTIME_CACHE);
    cache.put(event.request, network.clone());
    return network;
  } catch (err) {
    // Try cached page, else offline fallback
    const cached = await caches.match(event.request);
    return cached || (await caches.match("/offline.html"));
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  const cached = await cache.match(request);

  const networkPromise = fetch(request)
    .then((response) => {
      // Cache only valid same-origin or CORS responses
      if (
        response &&
        response.status === 200 &&
        (response.type === "basic" || response.type === "cors")
      ) {
        cache.put(request, response.clone());
      }
      return response;
    })
    .catch(() => null);

  // Return cached first, else network, else offline fallback
  return cached || networkPromise || (await caches.match("/offline.html"));
}
