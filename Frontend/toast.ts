type Toast = { text: string; kind: 'push' | 'request'; installationId: string }

type ToastEvent =
  | { type: 'push'; text: string; installationId: string }
  | { type: 'request'; pushCountAtStart: number; text: string; installationId: string }
  | { type: 'dismiss'; toast: Toast }

export const initialToastState = { toast: null as Toast | null, pushCount: 0 }

export function updateToast(state: typeof initialToastState, event: ToastEvent): typeof initialToastState {
  switch (event.type) {
    case 'push':
      return {
        toast: { text: event.text, kind: 'push', installationId: event.installationId },
        pushCount: state.pushCount + 1,
      }
    case 'request':
      if (state.pushCount !== event.pushCountAtStart ||
          (state.toast?.kind === 'push' && state.toast.installationId === event.installationId)) return state
      return { ...state, toast: { text: event.text, kind: 'request', installationId: event.installationId } }
    case 'dismiss':
      return state.toast === event.toast ? { ...state, toast: null } : state
  }
}
