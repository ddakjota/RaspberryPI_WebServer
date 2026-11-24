import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'

const source = await readFile(new URL('../public/sw.js', import.meta.url), 'utf8')

async function receivePush(activeInstallation) {
  const handlers = new Map()
  const shown = []
  const messages = []
  runInNewContext(source, {
    self: {
      location: { origin: 'https://laundry.example.com' },
      addEventListener: (type, handler) => handlers.set(type, handler),
      registration: { showNotification: async title => shown.push(title) },
      clients: { matchAll: async () => [{ postMessage: message => messages.push(message.title) }] },
    },
    caches: { open: async () => null },
    fetch: async url => {
      assert.equal(url, '/api/v1/me')
      return activeInstallation
        ? Response.json({ installation_id: activeInstallation })
        : new Response(null, { status: 401 })
    },
    AbortController, URL, setTimeout, clearTimeout, console,
  })

  let done
  handlers.get('push')({
    data: { json: () => ({ title: '세탁 완료', body: '세탁물을 확인하세요.', id: 'demo', installation_id: 'original' }) },
    waitUntil: promise => { done = promise },
  })
  await done
  return { shown, messages }
}

test('현재 기기에만 시스템 알림과 앱 내 알림을 표시한다', async () => {
  assert.deepEqual(await receivePush('original'), { shown: ['세탁 완료'], messages: ['세탁 완료'] })
  assert.deepEqual(await receivePush('replacement'), { shown: [], messages: [] })
  assert.deepEqual(await receivePush(null), { shown: [], messages: [] })
})
