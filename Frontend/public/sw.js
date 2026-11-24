self.addEventListener('install', event => event.waitUntil(self.skipWaiting()))

self.addEventListener('push', event => {
  if (!event.data) return
  const { title, body, id, installation_id } = event.data.json()
  if (!installation_id) return
  event.waitUntil((async () => {
    const key = new URL('/notification/' + encodeURIComponent(id), self.location.origin).href
    const cache = await caches.open('laundry-notifications-v1').catch(() => null)
    if (await cache?.match(key).catch(() => null)) return

    if (!(await isCurrentRecipient(installation_id))) return

    let displayed = false
    let displayError
    try {
      await self.registration.showNotification(title, { body, tag: id, icon: '/icon-192.png', renotify: false })
      displayed = true
    } catch (error) {
      console.warn('Laundry system notification failed', error)
      displayError = error.name + ': ' + error.message
    }
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const client of windows) {
      client.postMessage({ type: 'laundry-push', title, body, displayed, displayError, installation_id })
    }

    if (!cache || !displayed) return
    try {
      await cache.put(key, new Response('', { headers: { 'X-Delivered-At': String(Date.now()) } }))
      for (const request of await cache.keys()) {
        const record = await cache.match(request)
        if (record && Date.now() - Number(record.headers.get('X-Delivered-At')) > 86400000) await cache.delete(request)
      }
    } catch (error) {
      console.warn('Laundry notification cache unavailable', error)
    }
  })())
})

async function isCurrentRecipient(installationId) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 5000)
  try {
    const response = await fetch('/api/v1/me', { cache: 'no-store', signal: controller.signal })
    return response.ok && (await response.json()).installation_id === installationId
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

self.addEventListener('notificationclick', event => {
  event.notification.close()
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const existing = windows.find(client => client.url === new URL('/', self.location.origin).href)
    if (existing) await existing.focus()
    else await self.clients.openWindow('/')
  })())
})
