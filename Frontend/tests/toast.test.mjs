import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../toast.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
})
const { initialToastState, updateToast } = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'))

test('늦은 요청 확인이 실제 푸시 수신 메시지를 덮어쓰거나 되살아나지 않는다', () => {
  const received = updateToast(initialToastState, { type: 'push', installationId: 'first', text: '수신 완료' })
  const response = { type: 'request', installationId: 'first', pushCountAtStart: 0, text: '요청 완료' }
  assert.equal(updateToast(received, response), received)

  const dismissed = updateToast(received, { type: 'dismiss', toast: received.toast })
  assert.equal(dismissed.toast, null)
  assert.equal(updateToast(dismissed, response), dismissed)
})

test('요청 확인이 먼저 뜨면 실제 푸시로 바뀌고 이전 타이머가 새 알림을 닫지 않는다', () => {
  const requested = updateToast(initialToastState, { type: 'request', installationId: 'first', pushCountAtStart: 0, text: '요청 완료' })
  assert.equal(requested.toast.text, '요청 완료')
  const received = updateToast(requested, { type: 'push', installationId: 'first', text: '수신 완료' })
  assert.equal(received.toast.text, '수신 완료')
  assert.equal(updateToast(received, { type: 'dismiss', toast: requested.toast }), received)
})
