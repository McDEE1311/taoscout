'use strict';
const CACHE = 'taoscout-research-shell-v1';
const assets = ['./', './style.css', './app.js', './manifest.webmanifest', './icon-192.png', './icon-512.png'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(assets))));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('taoscout-research-shell-') && key !== CACHE).map(key => caches.delete(key))))));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  const allowed = assets.some(asset => new URL(asset, self.registration.scope).href === url.href);
  // API, authentication, and billing responses are NEVER cached.
  if (event.request.method !== 'GET' || !allowed) return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
