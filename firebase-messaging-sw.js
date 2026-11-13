/* Prop Lab: shows alert notifications when the app is closed (phones and computers). */
self.window = self;   // config.js sets window.PROPLAB_CONFIG
importScripts(
  'https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js',
  'https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js',
  'config.js'
);
firebase.initializeApp(self.PROPLAB_CONFIG.firebase);
const messaging = firebase.messaging();
// Messages with a notification part are shown by Firebase itself. This covers data-only ones.
messaging.onBackgroundMessage(p => {
  if (p.notification) return;
  const d = p.data || {};
  return self.registration.showNotification(d.title || 'Prop Lab', {body: d.body || '', icon: 'icon-192.png', badge: 'icon-192.png', data: {url: d.url || './'}});
});
self.addEventListener('notificationclick', e => {
  const url = e.notification.data && e.notification.data.url;
  if (!url) return;            // Firebase handles its own notifications
  e.notification.close();
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(ws => {
    for (const w of ws) if ('focus' in w) { w.navigate(url); return w.focus(); }
    return clients.openWindow(url);
  }));
});
