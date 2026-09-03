const CACHE_VERSION = "miso-gallery-v2";
const STATIC_CACHE = "miso-gallery-static-v2";

// Only unauthenticated static assets are pre-cached. Authenticated
// navigation responses (/, /recent, /trash, /settings, /about) are
// never written to the cache (Issue #453).
const CORE_ASSETS = [
  "/manifest.webmanifest",
  "/assets/style.css",
  "/assets/app.js",
  "/favicon.ico",
  "/service-worker.js",
];

const STATIC_URL_PATTERNS = [
  /^\/manifest\.webmanifest$/,
  /^\/assets\//,
  /^\/favicon\.ico$/,
  /^\/service-worker\.js$/,
];

function isStaticAsset(url) {
  return STATIC_URL_PATTERNS.some((pattern) => pattern.test(url.pathname));
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(CORE_ASSETS))
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== STATIC_CACHE)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

// Purge every cache when the auth state changes (login/logout/OIDC
// callback). The landing page posts { type: "auth_changed" } (Issue #453).
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "auth_changed") {
    event.waitUntil(
      caches.keys().then((keys) =>
        Promise.all(keys.map((key) => caches.delete(key)))
      )
    );
  }
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    // Authenticated HTML pages must never be cached (Issue #453):
    // network-first with no cache write.
    event.respondWith(
      fetch(request).catch(() =>
        caches.match(request).then((cached) => cached || caches.match("/"))
      )
    );
    return;
  }

  if (request.destination === "image") {
    // Thumbnails and images may embed user-specific metadata, so they
    // are served network-only and never cached (Issue #453).
    event.respondWith(fetch(request));
    return;
  }

  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((response) => {
            if (response.status === 200) {
              const copy = response.clone();
              caches
                .open(STATIC_CACHE)
                .then((cache) => cache.put(request, copy));
            }
            return response;
          })
      )
    );
  }
});
