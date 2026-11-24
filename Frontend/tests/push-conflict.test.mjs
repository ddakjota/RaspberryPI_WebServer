import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

async function loadApi(t, globals) {
  for (const [name, value] of Object.entries(globals)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { configurable: true, value })
    t.after(() => previous ? Object.defineProperty(globalThis, name, previous) : Reflect.deleteProperty(globalThis, name))
  }
  const source = await readFile(new URL('../api.ts', import.meta.url), 'utf8')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  })
  const url = 'data:text/javascript;base64,' + Buffer.from(outputText).toString('base64')
  return import(url + '#' + encodeURIComponent(t.name))
}

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

test('만료된 쿠키로 재등록하면 기존 구독 충돌만 한 번 복구한다', async t => {
  const key = new Uint8Array(65)
  key[0] = 4
  const calls = []
  let errorCode = 'SUBSCRIPTION_CONFLICT'
  const subscription = endpoint => ({
    endpoint,
    options: { applicationServerKey: key.buffer },
    unsubscribe: async () => { calls.push('unsubscribe') },
    toJSON: () => ({ endpoint }),
  })
  const manager = {
    getSubscription: async () => subscription('old'),
    subscribe: async () => { calls.push('subscribe'); return subscription('new') },
  }
  const registration = { pushManager: manager }
  const notification = { requestPermission: async () => 'granted' }
  const globals = {
    Notification: notification,
    window: { Notification: notification, PushManager: {} },
    navigator: { serviceWorker: { register: async () => registration, ready: Promise.resolve(registration) } },
    fetch: async (path, options) => {
      if (path.endsWith('/public-key')) return Response.json({ public_key: Buffer.from(key).toString('base64url') })
      assert.equal(path, '/api/v1/push/settings')
      const { subscription: { endpoint } } = JSON.parse(options.body)
      calls.push(endpoint)
      if (endpoint === 'old') return Response.json({ error: { code: errorCode } }, { status: 409 })
      return Response.json({ notifications_enabled: true })
    },
  }
  const { setPushEnabled } = await loadApi(t, globals)
  await setPushEnabled(true)
  assert.deepEqual(calls, ['old', 'unsubscribe', 'subscribe', 'new'])
  calls.length = 0
  errorCode = 'STATE_CONFLICT'
  await assert.rejects(setPushEnabled(true), { code: 'STATE_CONFLICT' })
  assert.deepEqual(calls, ['old'])
})

test('다른 탭에서 다시 켜면 이전 구독 해제가 끝난 후 새로 연결한다', { timeout: 3000 }, async t => {
  const key = new Uint8Array(65)
  key[0] = 4
  const calls = []
  const cleanup = deferred()
  const unsubscribing = deferred()
  let permissionRequested = false
  let lock = Promise.resolve()
  const subscription = endpoint => ({
    options: { applicationServerKey: key.buffer },
    toJSON: () => ({ endpoint }),
    unsubscribe: async () => {
      calls.push('unsubscribe')
      unsubscribing.resolve()
      await cleanup.promise
      activeSubscription = null
    },
  })
  let activeSubscription = subscription('old')
  const registration = { pushManager: {
    getSubscription: async () => activeSubscription,
    subscribe: async () => {
      calls.push('subscribe')
      activeSubscription = subscription('new')
      return activeSubscription
    },
  } }
  const notification = { requestPermission: async () => { permissionRequested = true; return 'granted' } }
  const globals = {
    Notification: notification,
    window: { Notification: notification, PushManager: {} },
    navigator: {
      serviceWorker: { register: async () => registration, ready: Promise.resolve(registration) },
      locks: { request: (name, options, callback) => {
        assert.equal(name, 'laundry-push-settings')
        assert.equal(options.signal.aborted, false)
        const result = lock.then(callback)
        lock = result.catch(() => {})
        return result
      } },
    },
    fetch: async (path, options) => {
      if (path.endsWith('/public-key')) return Response.json({ public_key: Buffer.from(key).toString('base64url') })
      assert.equal(path, '/api/v1/push/settings')
      const data = JSON.parse(options.body)
      if (data.notifications_enabled) calls.push(data.subscription.endpoint)
      else assert.equal(data.disconnect_subscription, true)
      calls.push(data.notifications_enabled ? 'enabled' : 'disabled')
      return Response.json({ notifications_enabled: data.notifications_enabled })
    },
  }
  const { setPushEnabled } = await loadApi(t, globals)
  const disable = setPushEnabled(false)
  await unsubscribing.promise
  const enable = setPushEnabled(true)
  assert.equal(permissionRequested, true)
  assert.deepEqual(calls, ['disabled', 'unsubscribe'])
  cleanup.resolve()
  await Promise.all([disable, enable])
  assert.deepEqual(calls, ['disabled', 'unsubscribe', 'subscribe', 'new', 'enabled'])
})

test('서비스 워커 활성화가 멈추면 연결 요청을 종료하고 잠금을 풀어준다', { timeout: 3000 }, async t => {
  const key = new Uint8Array(65)
  key[0] = 4
  const waiting = deferred()
  const stalledWorker = deferred()
  const registration = {}
  const notification = { requestPermission: async () => 'granted' }
  let lock = Promise.resolve()
  let expire
  let settingsRequests = 0
  const realSetTimeout = globalThis.setTimeout
  const { setPushEnabled } = await loadApi(t, {
    Notification: notification,
    window: { Notification: notification, PushManager: {} },
    setTimeout: (callback, delay, ...args) => {
      if (delay === 15000) expire = callback
      return realSetTimeout(callback, delay, ...args)
    },
    navigator: {
      serviceWorker: {
        register: async () => registration,
        get ready() { waiting.resolve(); return stalledWorker.promise },
      },
      locks: { request: (_name, _options, callback) => {
        const result = lock.then(callback)
        lock = result.catch(() => {})
        return result
      } },
    },
    fetch: async path => {
      if (path.endsWith('/public-key')) return Response.json({ public_key: Buffer.from(key).toString('base64url') })
      settingsRequests++
      return Response.json({ notifications_enabled: false })
    },
  })
  const enabling = setPushEnabled(true)
  await waiting.promise
  const rejected = assert.rejects(enabling, /다시 시도해 주세요/)
  expire()
  await rejected
  assert.deepEqual(await setPushEnabled(false), { notifications_enabled: false })
  assert.equal(settingsRequests, 1)
  stalledWorker.resolve(registration)
})
