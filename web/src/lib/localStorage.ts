/**
 * localStorage persistence for dashboard state.
 */

const STORAGE_KEY = 'kalshi-edge-engine'

export interface PersistedState {
  selectedTicker: string | null
  activePaperTab: 'summary' | 'intents' | 'fills' | 'positions' | 'config'
}

const DEFAULT_STATE: PersistedState = {
  selectedTicker: null,
  activePaperTab: 'summary',
}

export function loadPersistedState(): PersistedState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_STATE
    const parsed = JSON.parse(raw) as Record<string, unknown>
    const activePaperTab = parsed.activePaperTab
    const validPaperTab =
      activePaperTab === 'summary'
      || activePaperTab === 'intents'
      || activePaperTab === 'fills'
      || activePaperTab === 'positions'
      || activePaperTab === 'config'

    return {
      selectedTicker: typeof parsed.selectedTicker === 'string' ? parsed.selectedTicker : null,
      activePaperTab: validPaperTab ? activePaperTab : DEFAULT_STATE.activePaperTab,
    }
  } catch {
    return DEFAULT_STATE
  }
}

export function savePersistedState(state: PersistedState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Silently fail if localStorage is unavailable
  }
}
