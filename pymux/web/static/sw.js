// The service worker of pymux web: what makes a notification on Android,
// where a page cannot show one itself (`new Notification` throws), and
// what a click on one does. Lillecarl/pyterm#1073.
//
// A click brings the page forward and tells it which notification was
// clicked; the page tells the server, which shows that pane. With no
// page open, it opens the address the notification came from, and the
// page reads `notification` from it.

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const { id, url } = event.notification.data ?? {};
  event.waitUntil(
    (async () => {
      const pages = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const page = pages.find((one) => one.url === url) ?? pages[0];
      if (page) {
        await page.focus();
        page.postMessage({ type: "notification-click", id });
        return;
      }
      if (url) {
        const where = new URL(url);
        where.searchParams.set("notification", id);
        await self.clients.openWindow(where.toString());
      }
    })(),
  );
});
