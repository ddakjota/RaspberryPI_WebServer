export interface Connection {
  installation_id: string
  sensor_id: string
  notifications_enabled: boolean
  can_control?: boolean
}

export interface Device {
  demo?: boolean
  sensor_id: string
  status: 'idle' | 'running' | 'completed' | 'error'
  connection: 'online' | 'offline'
  monitoring_enabled: boolean
  started_at: string | null
  last_reading_at: string | null
  error_code: string | null
}

export interface Session {
  session_id: string
  started_at: string
  ended_at: string | null
  end_reason: 'completed' | 'cancelled' | 'server_restart' | null
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) { super(message) }
}

export async function api<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch('/api/v1' + path, {
    method,
    signal,
    cache: 'no-store',
    headers: { 'X-Laundry-Request': '1', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const data = await response.json().catch(() => null)
    throw new ApiError(response.status, data?.error?.message ?? '서버에 연결할 수 없습니다.', data?.error?.code)
  }
  return response.status === 204 ? undefined as T : await response.json()
}

export const serviceWorker = 'serviceWorker' in navigator
  ? navigator.serviceWorker.register('/sw.js').catch(() => null)
  : Promise.resolve(null)

export async function currentSubscription() {
  const registration = await serviceWorker
  return registration?.pushManager?.getSubscription() ?? null
}

async function pushPublicKey(signal?: AbortSignal) {
  const { public_key } = await api<{ public_key: string | null }>('/push/public-key', 'GET', undefined, signal)
  if (!public_key) return null
  const decoded = atob(public_key.replace(/-/g, '+').replace(/_/g, '/'))
  const key = Uint8Array.from(decoded, character => character.charCodeAt(0))
  if (key.length !== 65 || key[0] !== 4) throw new Error('서버의 Web Push 공개 키가 올바르지 않습니다.')
  return key
}

function usesKey(subscription: PushSubscription, key: Uint8Array) {
  const stored = subscription.options.applicationServerKey
  if (!stored || stored.byteLength !== key.length) return false
  return new Uint8Array(stored).every((byte, index) => byte === key[index])
}

export async function pushConnected() {
  if (!('Notification' in window) || Notification.permission !== 'granted') return false
  const subscription = await currentSubscription()
  if (!subscription) return false
  const { endpoint } = await api<{ endpoint: string | null }>('/push/subscription')
  if (endpoint !== subscription.endpoint) return false
  const key = await pushPublicKey()
  return key !== null && usesKey(subscription, key)
}

export async function setPushEnabled(enabled: boolean) {
  if (enabled) await requestPushPermission()
  return changePushRegistration(async (signal, canUnsubscribe) => {
    if (enabled) return registerPushSubscription(signal)
    const result = await api<Pick<Connection, 'notifications_enabled'>>('/push/settings', 'PATCH', {
      notifications_enabled: false,
      disconnect_subscription: true,
    }, signal)
    if (canUnsubscribe) await removeLocalSubscription(signal)
    return result
  })
}

export function unlinkDevice() {
  return changePushRegistration(async (signal, canUnsubscribe) => {
    await api('/me', 'DELETE', undefined, signal)
    if (canUnsubscribe) await removeLocalSubscription(signal)
  })
}

async function removeLocalSubscription(signal: AbortSignal) {
  try {
    const subscription = await untilAborted(currentSubscription(), signal)
    signal.throwIfAborted()
    await subscription?.unsubscribe()
  } catch (error) {
    if (!signal.aborted) console.warn('Could not remove the local push subscription', error)
  }
}

async function changePushRegistration<T>(change: (signal: AbortSignal, canUnsubscribe: boolean) => Promise<T>) {
  const controller = new AbortController()
  const { signal } = controller
  const timer = setTimeout(() => controller.abort(new Error('알림 설정 응답이 늦어졌습니다. 다시 시도해 주세요.')), 15000)
  const locks = navigator.locks
  function updateSettings() {
    signal.throwIfAborted()
    return change(signal, !!locks)
  }

  try {
    const operation = locks
      ? Promise.resolve(locks.request('laundry-push-settings', { signal }, updateSettings))
      : updateSettings()
    // A native subscribe/unsubscribe cannot be cancelled. Let it settle under the lock
    // while allowing the UI and any other waiting tab to stop waiting on their deadlines.
    return await untilAborted(operation, signal)
  } finally {
    clearTimeout(timer)
  }
}

function untilAborted<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason)
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    void operation.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort))
  })
}

async function requestPushPermission() {
  if (!('Notification' in window) || !('PushManager' in window)) {
    throw new Error('아이폰에서는 홈 화면에 앱을 추가한 뒤 알림을 연결해 주세요.')
  }
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') throw new Error('브라우저 설정에서 알림을 허용해 주세요.')
}

async function registerPushSubscription(signal: AbortSignal) {
  const key = await pushPublicKey(signal)
  if (!key) throw new Error('서버에 Web Push 키가 설정되지 않았습니다.')
  if (!(await untilAborted(serviceWorker, signal))) throw new Error('알림 서비스가 준비되지 않았습니다.')
  const registration = await untilAborted(navigator.serviceWorker.ready, signal)
  let subscription = await untilAborted(registration.pushManager.getSubscription(), signal)
  signal.throwIfAborted()

  if (subscription && !usesKey(subscription, key)) {
    await subscription.unsubscribe()
    subscription = null
  }
  function save(subscription: PushSubscription) {
    signal.throwIfAborted()
    return api<Pick<Connection, 'notifications_enabled'>>('/push/settings', 'PATCH', {
      notifications_enabled: true,
      subscription: subscription.toJSON(),
    }, signal)
  }
  const options = { userVisibleOnly: true, applicationServerKey: key }
  if (subscription) {
    try {
      return await save(subscription)
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 409 && error.code === 'SUBSCRIPTION_CONFLICT')) throw error
      signal.throwIfAborted()
      await subscription.unsubscribe()
    }
  }
  signal.throwIfAborted()
  return save(await registration.pushManager.subscribe(options))
}
