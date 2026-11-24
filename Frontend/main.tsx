import { useEffect, useReducer, useState, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { api, ApiError, pushConnected, setPushEnabled, unlinkDevice, type Connection, type Device, type Session } from './api'
import { initialToastState, updateToast } from './toast'
import './style.css'

const labels = {
  idle: '대기 중', running: '세탁 중', completed: '세탁 완료', error: '연결 오류', cancelled: '감지 중지',
}
const descriptions = {
  idle: '표시등이 켜지면 세탁을 자동으로 감지합니다.',
  running: '세탁이 끝나면 연결된 기기로 알림을 보냅니다.',
  completed: '세탁이 끝났습니다. 세탁물을 확인해 주세요.',
  error: '센서 전원과 Wi-Fi 연결을 확인해 주세요.',
  cancelled: '감지를 재시작하면 자동 감지가 다시 켜집니다.',
}
const reasons = { completed: '완료', cancelled: '감지 취소', server_restart: '서버 재시작' }
const formatTime = (value: string) => new Intl.DateTimeFormat('ko-KR', {
  month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
}).format(new Date(value))
const message = (error: unknown) => error instanceof Error ? error.message : '잠시 후 다시 시도해 주세요.'

function App() {
  const [me, setMe] = useState<Connection | null | undefined>(undefined)
  const [device, setDevice] = useState<Device | null>(null)
  const [sessions, setSessions] = useState<Session[] | null>(null)
  const [tab, setTab] = useState<'status' | 'history' | 'settings'>('status')
  const [connectedPush, setConnectedPush] = useState(false)
  const [pending, setPending] = useState(false)
  const [pushChange, setPushChange] = useState<'enabling' | 'disabling' | null>(null)
  const [{ toast, pushCount }, dispatchToast] = useReducer(updateToast, initialToastState)
  const [failure, setFailure] = useState('')
  const [refreshError, setRefreshError] = useState('')
  const [confirmUnlink, setConfirmUnlink] = useState(false)
  const [version, refresh] = useState(0)
  const id = me?.sensor_id
  const installation = me?.installation_id
  const devicePath = '/devices/' + encodeURIComponent(id ?? '')

  useEffect(() => {
    if (!('serviceWorker' in navigator)) return
    function onPush(event: MessageEvent) {
      if (installation && event.data?.type === 'laundry-push' && event.data.installation_id === installation) {
        const suffix = event.data.displayed ? '' : ' · 시스템 알림 표시에 실패했습니다. ' + (event.data.displayError ?? '')
        dispatchToast({ type: 'push', installationId: installation, text: event.data.title + ' · ' + event.data.body + suffix })
      }
    }
    navigator.serviceWorker.addEventListener('message', onPush)
    return () => navigator.serviceWorker.removeEventListener('message', onPush)
  }, [installation])

  useEffect(() => {
    if (!toast) return
    const timeout = window.setTimeout(() => dispatchToast({ type: 'dismiss', toast }), 4000)
    return () => window.clearTimeout(timeout)
  }, [toast])

  useEffect(() => {
    let active = true
    if (installation || me === null) return
    api<Connection>('/me').then(value => { if (active) setMe(value) }).catch(error => {
      if (!active) return
      if (error instanceof ApiError && error.status === 401) setMe(null)
      else setFailure(message(error))
    })
    return () => { active = false }
  }, [installation, me, version])

  useEffect(() => {
    if (!installation) return
    let active = true
    let timer: number
    async function poll() {
      try {
        const info = await api<Device>(devicePath + '/status')
        if (active) {
          setDevice(info)
          setRefreshError('')
        }
      } catch (error) {
        if (active && error instanceof ApiError && error.status === 401) {
          setDevice(null)
          setSessions(null)
          setMe(null)
        } else if (active) setRefreshError('상태를 갱신할 수 없습니다. 연결을 확인해 주세요.')
      } finally {
        if (active) timer = window.setTimeout(poll, 5000)
      }
    }
    void poll()
    return () => { active = false; window.clearTimeout(timer) }
  }, [installation, devicePath, version])

  useEffect(() => {
    if (!installation || tab !== 'settings' || pending) return
    let active = true
    let request = 0
    async function syncPushSettings() {
      const currentRequest = ++request
      try {
        const [connection, connected] = await Promise.all([api<Connection>('/me'), pushConnected()])
        if (active && currentRequest === request && connection.installation_id === installation) {
          setMe(previous => previous?.installation_id === installation ? connection : previous)
          setConnectedPush(connected)
        }
      } catch {
        // A failed refresh does not change the last confirmed setting.
      }
    }
    void syncPushSettings()
    window.addEventListener('focus', syncPushSettings)
    return () => {
      active = false
      window.removeEventListener('focus', syncPushSettings)
    }
  }, [installation, tab, pending])

  useEffect(() => {
    if (!installation || tab !== 'history') return
    let active = true
    api<{ sessions: Session[] }>(devicePath + '/sessions')
      .then(value => { if (active) setSessions(value.sessions) })
      .catch(error => { if (active) setFailure(message(error)) })
    return () => { active = false }
  }, [installation, tab, devicePath, device?.status])

  async function act(action: () => Promise<void>) {
    setPending(true)
    setFailure('')
    try { await action() }
    catch (error) { setFailure(message(error)) }
    finally { setPending(false) }
  }

  function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const fields = new FormData(event.currentTarget)
    void act(async () => {
      await api('/registrations', 'POST', {
        sensor_id: String(fields.get('sensor_id')).trim(),
        registration_code: String(fields.get('registration_code')).trim(),
      })
      setConnectedPush(false)
      setMe(await api<Connection>('/me'))
      setTab('status')
    })
  }

  const monitor = device?.monitoring_enabled ?? true
  const canControl = me?.can_control !== false
  const state = device ? monitor ? device.status : 'cancelled' : undefined

  return <main className="shell">
    <header className="brand">
      <img src="/icon.svg" alt="" width="44" height="44" />
      <h1>세탁 알리미</h1>
      {id && <span className="sensor-id">센서 {id}</span>}
    </header>

    {failure && <p role="alert" className="feedback error">{failure}</p>}
    <div className="toast-region" role="status">
      {toast && toast.installationId === installation && <p className="toast">{toast.text}</p>}
    </div>

    {me === undefined && <section className="panel">
      {failure ? <button onClick={() => { setFailure(''); refresh(v => v + 1) }}>다시 시도</button> : <p role="status">연결 정보를 확인하고 있어요.</p>}
    </section>}

    {me === null && <section className="panel registration">
      <div className="intro">
        <img src="/icon.svg" alt="" width="88" height="88" />
        <h2>세탁이 끝나면<br />알려드릴게요.</h2>
        <p>센서를 연결하고 세탁기 상태를 확인하세요.</p>
      </div>
      <form onSubmit={register}>
        <label>센서 ID<input name="sensor_id" placeholder="센서 ID 입력" maxLength={100} autoCapitalize="off" required /></label>
        <label>등록 코드<input name="registration_code" type="password" placeholder="등록 코드 입력" maxLength={256} required /></label>
        <button className="primary" disabled={pending}>{pending ? '연결 중…' : '센서 연결'}</button>
      </form>
    </section>}

    {me && <>
      <nav aria-label="앱 메뉴">
        {([['status', '현재 상태'], ['history', '작업 기록'], ['settings', '설정']] as const).map(([key, label]) =>
          <button key={key} aria-current={tab === key ? 'page' : undefined}
            onClick={() => { setTab(key); setFailure(''); setConfirmUnlink(false) }}>{label}</button>)}
      </nav>

      {tab === 'status' && <>
        {refreshError && <p role="alert" className="feedback error">{refreshError}</p>}
        <section className="hero">
          <div>
            <span className="connection"><i data-online={device?.connection === 'online'} />
              {device?.connection === 'online' ? '센서 연결됨' : device ? '센서 오프라인' : '센서 확인 중'}
            </span>
            <h2>{state ? labels[state] : '상태 확인 중'}</h2>
            {state && <p>{descriptions[state]}</p>}
          </div>
          <img src="/icon.svg" width="124" height="124" alt="" />
        </section>
        {device?.demo && canControl && <section className="panel">
          <h3>테스트용 센서</h3>
          <p>실물 센서 없이 세탁 상태와 완료 알림을 확인하세요.</p>
          <div className="actions">
            {(['start', 'complete'] as const).map(action =>
              <button key={action} disabled={pending || !monitor || (device.status === 'running') === (action === 'start')}
                onClick={() => void act(async () => {
                  await api(devicePath + '/demo/' + action, 'POST')
                  setDevice(await api<Device>(devicePath + '/status'))
                  refresh(v => v + 1)
                })}>{action === 'start' ? '세탁 시작' : '세탁 완료'}</button>)}
          </div>
        </section>}
        <section className="panel control">
          <div>
            <h3>자동 감지</h3>
            <p>{device?.last_reading_at ? '최근 수신 · ' + formatTime(device.last_reading_at) : '아직 수신한 밝기 데이터가 없어요.'}</p>
            {device?.started_at && <p>세탁 시작 · {formatTime(device.started_at)}</p>}
          </div>
          {canControl && <button disabled={pending || !device} onClick={() => void act(async () => {
            const result = await api<Pick<Device, 'status' | 'monitoring_enabled'>>(devicePath + (monitor ? '/cancel' : '/resume'), 'POST')
            setDevice(previous => previous && { ...previous, ...result, started_at: null, error_code: null })
            refresh(v => v + 1)
          })}>{monitor ? '감지 취소' : '감지 재시작'}</button>}
        </section>
      </>}

      {tab === 'history' && <section className="panel">
        <h2>작업 기록</h2><p className="muted">최근 30일</p>
        {!sessions ? <p>{failure ? '기록을 불러오지 못했습니다.' : '기록을 불러오는 중…'}</p>
          : !sessions.length ? <p>아직 세탁 기록이 없어요.</p>
          : <ul className="sessions">{sessions.map(session => <li key={session.session_id}>
            <div><strong>{formatTime(session.started_at)}</strong>
              {session.ended_at && <small>종료 · {formatTime(session.ended_at)}</small>}</div>
            <span>{session.end_reason ? reasons[session.end_reason] ?? session.end_reason : '진행 중'}</span>
          </li>)}</ul>}
      </section>}

      {tab === 'settings' && <>
        <section className="panel">
          <div className="control"><div><h2>푸시 알림</h2>
            <p role="status">{pushChange === 'enabling' ? '알림 연결 중…' :
              pushChange === 'disabling' ? '알림 끄는 중…' : '이 기기의 완료·연결 알림을 설정합니다.'}</p>
          </div>
            <label className="switch" data-loading={pushChange !== null}><input type="checkbox" checked={me.notifications_enabled && connectedPush} disabled={pending}
              aria-label="이 기기 알림" onChange={event => {
                const enabled = event.target.checked
                setPushChange(enabled ? 'enabling' : 'disabling')
                void act(async () => {
                  const result = await setPushEnabled(enabled)
                  setMe(previous => previous && { ...previous, ...result })
                  setConnectedPush(result.notifications_enabled)
                }).finally(() => setPushChange(null))
              }} /><span aria-hidden="true" /></label>
          </div>
          <div className="actions">
            <button disabled={pending || !connectedPush} onClick={() => void act(async () => {
              await api('/push/test', 'POST')
              dispatchToast({
                type: 'request', pushCountAtStart: pushCount,
                installationId: me.installation_id, text: '테스트 알림을 요청했습니다.',
              })
            })}>테스트 알림</button>
          </div>
          <p className="muted">아이폰에서는 Safari 공유 메뉴에서 홈 화면에 추가한 뒤 앱을 열어 주세요.</p>
        </section>
        <section className="panel control">
          <div><h3>센서 연결</h3><p>{confirmUnlink ? '이 기기의 센서 연결을 해제할까요?' : id}</p></div>
          <div className="actions">
            {confirmUnlink && <button onClick={() => setConfirmUnlink(false)}>닫기</button>}
            <button className="danger" disabled={pending} onClick={() => {
              if (!confirmUnlink) { setConfirmUnlink(true); return }
              void act(async () => {
                await unlinkDevice()
                setMe(null)
                setDevice(null)
                setSessions(null)
                setConnectedPush(false)
                setRefreshError('')
                setConfirmUnlink(false)
                setTab('status')
              })
            }}>{confirmUnlink ? '해제 확인' : '연결 해제'}</button>
          </div>
        </section>
      </>}
    </>}
  </main>
}

createRoot(document.getElementById('root')!).render(<App />)
