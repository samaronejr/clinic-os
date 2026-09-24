{% autoescape off %}/* Clinic OS shell worker: versioned static assets only.
   Documents, product routes, authenticated responses and clinical data are
   never intercepted and never stored; offline navigation fails closed. */
"use strict";

const VERSION = "{{ version|escapejs }}";
// Retire the earlier cache policy, including any credential-bearing entries.
const CACHE_NAME = "clinic-os-static-anonymous-v2-" + VERSION;
const STATIC_PREFIX = "/static/";
const PRECACHE = {{ precache_json }};
const VERSIONED_ASSETS = new Set(PRECACHE.map((url) => new URL(url, self.location.origin).href));
const STATIC_DESTINATIONS = new Set(["style", "script", "image", "font", "manifest", ""]);

function isCacheableStaticRequest(request) {
  // Cookie headers are browser-managed and may be hidden from the worker.
  // Only credentials=omit guarantees that no session cookie is sent.
  if (request.method !== "GET" || request.mode === "navigate" ||
      request.credentials !== "omit" || request.headers.has("Authorization") ||
      request.headers.has("Cookie") || request.headers.has("Proxy-Authorization")) {
    return false;
  }
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith(STATIC_PREFIX) ||
      url.username || url.password || !VERSIONED_ASSETS.has(url.href)) {
    return false;
  }
  return STATIC_DESTINATIONS.has(request.destination);
}

function isStorableResponse(response) {
  if (!response.ok || response.type !== "basic") {
    return false;
  }
  const cacheControl = (response.headers.get("Cache-Control") || "").toLowerCase();
  if (cacheControl.includes("no-store") || cacheControl.includes("private")) {
    return false;
  }
  const contentType = (response.headers.get("Content-Type") || "").toLowerCase();
  return !contentType.startsWith("text/html");
}

async function staticFirst(request) {
  // Never persist caller headers, even on an otherwise anonymous request.
  request = new Request(request.url, {credentials: "omit", redirect: "error"});
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) {
    return cached;
  }
  const response = await fetch(request);
  if (isStorableResponse(response)) {
    await cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE.map((url) =>
        new Request(url, {credentials: "omit", redirect: "error"})
      )))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (isCacheableStaticRequest(event.request)) {
    event.respondWith(staticFirst(event.request));
  }
});
{% endautoescape %}
