'use strict';
// Synthetic push handler: the visible text is fixed, so a push payload can
// never put clinical content on a lock screen (SC-7).
self.addEventListener('push', (event) => {
  event.waitUntil(
    self.registration
      .showNotification('Clinic Ops', {
        body: 'You have a new notification.',
        tag: 'device-capabilities',
        data: {kind: 'generic'},
      })
      .then(() => self.clients.matchAll())
      .then((clients) => clients.forEach((client) => client.postMessage('shown'))),
  );
});
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
