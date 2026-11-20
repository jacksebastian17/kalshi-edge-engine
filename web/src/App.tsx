import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  PaperConfigResponse,
  PaperFill,
  PaperIntent,
  PaperIntentDebugResponse,
  PaperMetricsResponse,
  PaperPnlSnapshot,
  PaperPosition,
  PaperSummaryResponse,
  ScannerStatusResponse,
  SignalResponse,
} from './lib/api'
import { OrderbookDrawer } from './components/OrderbookDrawer'
import { SignalsTable } from './components/SignalsTable'
import { APIErrorBanner, StatusBanner, useHealthCheck } from './components/StatusBanner'
import { loadPersistedState, savePersistedState } from './lib/localStorage'

const MAX_CHART_POINTS = 300
const MAX_PNL_POINTS = 20

type EdgePoint = {
  t: number
  maxEdge: number
  netEdgeYes: number | null
  netEdgeNo: number | null
  gap: number | null
}

type ScannerState = 'stopped' | 'starting' | 'running' | 'stopping' | 'error'
type PaperTradingState = 'stopped' | 'starting' | 'running' | 'stopping' | 'error'
type PaperTab = 'summary' | 'intents' | 'fills' | 'positions' | 'config'
type FillSortDirection = 'desc' | 'asc'

type PnlHistoryPoint = {
  tsMs: number
  totalPnlCents: number
}

function getMaxEdge(signal: SignalResponse): number {
  const yes = signal.net_edge_yes ?? Number.NEGATIVE_INFINITY
  const no = signal.net_edge_no ?? Number.NEGATIVE_INFINITY
  const max = Math.max(yes, no)
  return Number.isFinite(max) ? max : 0
}

function formatMetric(value: number | null | undefined): string {
  if (value == null) return '—'
  return value.toLocaleString()
}

function formatCurrencyCents(value: number | null | undefined): string {
  if (value == null) return '—'
  const dollars = value / 100
  return dollars.toLocaleString(undefined, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function formatTimestamp(tsMs: number | null | undefined): string {
  if (tsMs == null) return '—'
  return new Date(tsMs).toLocaleTimeString()
}

function formatAgeMs(tsMs: number | null | undefined): string {
  if (tsMs == null) return '—'
  const seconds = Math.max(0, Math.floor((Date.now() - tsMs) / 1000))
  return `${seconds}s`
}

function failedCount(value: ScannerStatusResponse['tickers_failed_last_cycle'] | null | undefined): number | null {
  if (Array.isArray(value)) {
    return value.length
  }
  if (typeof value === 'number') {
    return value
  }
  return null
}

function formatUniverse(universe: string | null | undefined): string {
  const normalized = universe?.trim().toUpperCase()
  return normalized && normalized.length > 0 ? normalized : '—'
}

function UniverseBadge({ universe }: { universe: string | null | undefined }) {
  const value = formatUniverse(universe)
  const classes =
    value === 'NBA'
      ? 'bg-blue-900/60 text-blue-200 border-blue-700/50'
      : value === 'CBB'
        ? 'bg-amber-900/60 text-amber-200 border-amber-700/50'
        : 'bg-zinc-800 text-zinc-300 border-white/10'

  return (
    <span className={`inline-block px-2 py-0.5 text-[11px] rounded border ${classes}`}>
      {value}
    </span>
  )
}

function ScannerStatusStrip({
  status,
}: {
  status: ScannerStatusResponse | null
}) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-3 border border-white/10 bg-zinc-900/50 rounded p-3 text-xs">
      <div>
        <p className="text-zinc-500">Discovered</p>
        <p className="text-zinc-200 tabular-nums">{formatMetric(status?.markets_discovered)}</p>
      </div>
      <div>
        <p className="text-zinc-500">Scanned/Cycle</p>
        <p className="text-zinc-200 tabular-nums">
          {formatMetric(status?.markets_scanned_last_cycle)}
        </p>
      </div>
      <div>
        <p className="text-zinc-500">Cycle ms</p>
        <p className="text-zinc-200 tabular-nums">
          {formatMetric(status?.last_cycle_duration_ms)}
        </p>
      </div>
      <div>
        <p className="text-zinc-500">Snapshot Age ms</p>
        <p className="text-zinc-200 tabular-nums">{formatMetric(status?.snapshot_age_ms)}</p>
      </div>
      <div>
        <p className="text-zinc-500">Failed</p>
        <p className="text-zinc-200 tabular-nums">{formatMetric(failedCount(status?.tickers_failed_last_cycle))}</p>
      </div>
    </div>
  )
}

function MiniPnlChart({ points }: { points: PnlHistoryPoint[] }) {
  if (points.length < 2) {
    return (
      <div className="h-28 border border-white/10 rounded bg-zinc-900/40 flex items-center justify-center text-xs text-zinc-500">
        Waiting for PnL snapshots...
      </div>
    )
  }

  const width = 560
  const height = 112
  const min = Math.min(...points.map((p) => p.totalPnlCents))
  const max = Math.max(...points.map((p) => p.totalPnlCents))
  const span = Math.max(1, max - min)

  const polyline = points
    .map((point, index) => {
      const x = (index / Math.max(1, points.length - 1)) * (width - 8) + 4
      const normalized = (point.totalPnlCents - min) / span
      const y = height - 4 - normalized * (height - 8)
      return `${x},${y}`
    })
    .join(' ')

  const latest = points[points.length - 1]
  const strokeClass = latest.totalPnlCents >= 0 ? 'stroke-emerald-400' : 'stroke-red-400'

  return (
    <div className="border border-white/10 rounded bg-zinc-900/40 p-2">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-28">
        <polyline
          fill="none"
          className={strokeClass}
          strokeWidth="2"
          points={polyline}
        />
      </svg>
      <div className="mt-2 text-[11px] text-zinc-500 flex justify-between">
        <span>Min {formatCurrencyCents(min)}</span>
        <span>Max {formatCurrencyCents(max)}</span>
      </div>
    </div>
  )
}

export default function App() {
  const [signals, setSignals] = useState<SignalResponse[]>([])
  const [isSignalsLoading, setIsSignalsLoading] = useState(false)
  const [scannerState, setScannerState] = useState<ScannerState>('stopped')
  const [paperTradingState, setPaperTradingState] = useState<PaperTradingState>('stopped')
  const [activePaperTab, setActivePaperTab] = useState<PaperTab>('summary')
  const [paperSummary, setPaperSummary] = useState<PaperSummaryResponse | null>(null)
  const [paperMetrics, setPaperMetrics] = useState<PaperMetricsResponse | null>(null)
  const [paperPnlSnapshot, setPaperPnlSnapshot] = useState<PaperPnlSnapshot | null>(null)
  const [paperOpenIntents, setPaperOpenIntents] = useState<PaperIntent[]>([])
  const [paperFills, setPaperFills] = useState<PaperFill[]>([])
  const [paperPositions, setPaperPositions] = useState<PaperPosition[]>([])
  const [paperConfig, setPaperConfig] = useState<PaperConfigResponse | null>(null)
  const [selectedIntentId, setSelectedIntentId] = useState<string | null>(null)
  const [paperIntentDebug, setPaperIntentDebug] = useState<PaperIntentDebugResponse | null>(null)
  const [paperDebugLoading, setPaperDebugLoading] = useState(false)
  const [paperFillSortDirection, setPaperFillSortDirection] = useState<FillSortDirection>('desc')
  const [paperPnlHistory, setPaperPnlHistory] = useState<PnlHistoryPoint[]>([])
  const [scannerStatus, setScannerStatus] = useState<ScannerStatusResponse | null>(null)
  const [scannerInfoMessage, setScannerInfoMessage] = useState<string | null>(null)
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null)
  const [selectedSignal, setSelectedSignal] = useState<SignalResponse | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [apiError, setApiError] = useState<string | null>(null)
  const [chartByTicker, setChartByTicker] = useState<Record<string, EdgePoint[]>>({})
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  const hasRestoredRef = useRef(false)

  const statusInFlightRef = useRef(false)
  const topSignalsInFlightRef = useRef(false)
  const paperSummaryInFlightRef = useRef(false)
  const paperPnlInFlightRef = useRef(false)
  const paperIntentsInFlightRef = useRef(false)
  const paperFillsInFlightRef = useRef(false)
  const paperPositionsInFlightRef = useRef(false)
  const paperConfigInFlightRef = useRef(false)
  const paperMetricsInFlightRef = useRef(false)

  const statusPollAbortRef = useRef<AbortController | null>(null)
  const topSignalsPollAbortRef = useRef<AbortController | null>(null)
  const paperSummaryAbortRef = useRef<AbortController | null>(null)
  const paperPnlAbortRef = useRef<AbortController | null>(null)
  const paperIntentsAbortRef = useRef<AbortController | null>(null)
  const paperFillsAbortRef = useRef<AbortController | null>(null)
  const paperPositionsAbortRef = useRef<AbortController | null>(null)
  const paperConfigAbortRef = useRef<AbortController | null>(null)
  const paperMetricsAbortRef = useRef<AbortController | null>(null)
  const paperDebugAbortRef = useRef<AbortController | null>(null)

  const statusIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const topSignalsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const paperSummaryIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const paperPnlIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const paperIntentsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const paperFillsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const paperPositionsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const paperConfigIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const paperMetricsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const { isConnected, lastHeartbeat, error: healthError } = useHealthCheck(5000)

  const clearScannerPolling = useCallback(() => {
    if (statusIntervalRef.current) {
      clearInterval(statusIntervalRef.current)
      statusIntervalRef.current = null
    }
    if (topSignalsIntervalRef.current) {
      clearInterval(topSignalsIntervalRef.current)
      topSignalsIntervalRef.current = null
    }
    statusPollAbortRef.current?.abort()
    topSignalsPollAbortRef.current?.abort()
    statusPollAbortRef.current = null
    topSignalsPollAbortRef.current = null
    statusInFlightRef.current = false
    topSignalsInFlightRef.current = false
    setIsSignalsLoading(false)
  }, [])

  const clearPaperPolling = useCallback(() => {
    const intervals = [
      paperSummaryIntervalRef,
      paperPnlIntervalRef,
      paperIntentsIntervalRef,
      paperFillsIntervalRef,
      paperPositionsIntervalRef,
      paperConfigIntervalRef,
      paperMetricsIntervalRef,
    ]
    for (const intervalRef of intervals) {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }

    const abortRefs = [
      paperSummaryAbortRef,
      paperPnlAbortRef,
      paperIntentsAbortRef,
      paperFillsAbortRef,
      paperPositionsAbortRef,
      paperConfigAbortRef,
      paperMetricsAbortRef,
      paperDebugAbortRef,
    ]
    for (const abortRef of abortRefs) {
      abortRef.current?.abort()
      abortRef.current = null
    }

    paperSummaryInFlightRef.current = false
    paperPnlInFlightRef.current = false
    paperIntentsInFlightRef.current = false
    paperFillsInFlightRef.current = false
    paperPositionsInFlightRef.current = false
    paperConfigInFlightRef.current = false
    paperMetricsInFlightRef.current = false
  }, [])

  const updateSignals = useCallback(
    (incomingSignals: SignalResponse[]) => {
      setSignals(incomingSignals)
      setLastUpdated(Date.now())

      if (selectedTicker) {
        const selectedFromResponse = incomingSignals.find(
          (entry) => entry.ticker === selectedTicker
        )

        if (selectedFromResponse) {
          const nextPoint: EdgePoint = {
            t: Date.now(),
            maxEdge: getMaxEdge(selectedFromResponse),
            netEdgeYes: selectedFromResponse.net_edge_yes,
            netEdgeNo: selectedFromResponse.net_edge_no,
            gap: selectedFromResponse.imbalance_gap,
          }

          setChartByTicker((prev) => {
            const existing = prev[selectedFromResponse.ticker] ?? []
            const nextSeries = [...existing, nextPoint].slice(-MAX_CHART_POINTS)
            return {
              ...prev,
              [selectedFromResponse.ticker]: nextSeries,
            }
          })

          if (drawerOpen) {
            setSelectedSignal(selectedFromResponse)
          }
        }
      }
    },
    [selectedTicker, drawerOpen]
  )

  const startScannerRequest = useCallback(async (signal?: AbortSignal) => {
    setScannerState('starting')
    setScannerInfoMessage(null)
    setApiError(null)

    try {
      const response = await api.startScanner({ signal })
      if (response.reason === 'already_running') {
        setScannerInfoMessage('Scanner already running.')
      }
      setScannerState('running')
    } catch (err) {
      if (signal?.aborted) {
        return
      }
      const message = err instanceof Error ? err.message : 'Failed to start scanner'
      setApiError(message)
      setScannerState('error')
    }
  }, [])

  useEffect(() => {
    if (hasRestoredRef.current) return
    hasRestoredRef.current = true

    const restored = loadPersistedState()
    setSelectedTicker(restored.selectedTicker)
    setActivePaperTab(restored.activePaperTab)
  }, [])

  useEffect(() => {
    if (!hasRestoredRef.current) return
    savePersistedState({
      selectedTicker,
      activePaperTab,
    })
  }, [selectedTicker, activePaperTab])

  useEffect(() => {
    let active = true

    const hydrateScannerState = async () => {
      try {
        const status = await api.getScannerStatus()
        if (!active) return

        setScannerStatus(status)
        setScannerState(status.enabled ? 'running' : 'stopped')
      } catch (err) {
        if (!active) return
        const message = err instanceof Error ? err.message : 'Failed to fetch scanner status'
        setApiError(message)
      }
    }

    hydrateScannerState()

    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    if (scannerState !== 'running') {
      clearScannerPolling()
      return
    }

    let active = true

    const pollStatus = async () => {
      if (statusInFlightRef.current) return
      statusInFlightRef.current = true
      statusPollAbortRef.current?.abort()
      const controller = new AbortController()
      statusPollAbortRef.current = controller

      try {
        const status = await api.getScannerStatus({ signal: controller.signal })
        if (!active) return

        setScannerStatus(status)
        if (!status.enabled) {
          setScannerState('error')
          setApiError('Scanner is no longer enabled.')
          setScannerInfoMessage('Scanner stopped unexpectedly. Please start again.')
        }
      } catch (err) {
        if (controller.signal.aborted || !active) {
          return
        }
        const message = err instanceof Error ? err.message : 'Failed to fetch scanner status'
        setApiError(message)
      } finally {
        statusPollAbortRef.current = null
        statusInFlightRef.current = false
      }
    }

    const pollTopSignals = async () => {
      if (topSignalsInFlightRef.current) return
      topSignalsInFlightRef.current = true
      topSignalsPollAbortRef.current?.abort()
      const controller = new AbortController()
      topSignalsPollAbortRef.current = controller
      setIsSignalsLoading(true)

      try {
        const snapshot = await api.topSignals({ signal: controller.signal })
        if (!active) return

        updateSignals(snapshot.signals)
        setScannerInfoMessage((prev) =>
          prev === 'Top signals snapshot warming up...' ? null : prev
        )
      } catch (err) {
        if (controller.signal.aborted || !active) {
          return
        }

        const message = err instanceof Error ? err.message : 'Failed to fetch top signals'
        if (message.includes('(503)')) {
          setScannerInfoMessage('Top signals snapshot warming up...')
        } else {
          setApiError(message)
        }
      } finally {
        topSignalsPollAbortRef.current = null
        topSignalsInFlightRef.current = false
        setIsSignalsLoading(false)
      }
    }

    pollStatus()
    pollTopSignals()
    statusIntervalRef.current = setInterval(pollStatus, 2000)
    topSignalsIntervalRef.current = setInterval(pollTopSignals, 10000)

    return () => {
      active = false
      clearScannerPolling()
    }
  }, [scannerState, updateSignals, clearScannerPolling])

  useEffect(() => {
    if (paperTradingState !== 'running') {
      clearPaperPolling()
      return
    }

    let active = true

    const pollPaperSummary = async () => {
      if (paperSummaryInFlightRef.current) return
      paperSummaryInFlightRef.current = true
      paperSummaryAbortRef.current?.abort()
      const controller = new AbortController()
      paperSummaryAbortRef.current = controller

      try {
        const summary = await api.getPaperSummary({ signal: controller.signal })
        if (!active) return
        setPaperSummary(summary)
      } catch (err) {
        if (!controller.signal.aborted && active) {
          const message = err instanceof Error ? err.message : 'Failed to fetch paper summary'
          setApiError(message)
        }
      } finally {
        paperSummaryAbortRef.current = null
        paperSummaryInFlightRef.current = false
      }
    }

    const pollPaperPnlSnapshot = async () => {
      if (paperPnlInFlightRef.current) return
      paperPnlInFlightRef.current = true
      paperPnlAbortRef.current?.abort()
      const controller = new AbortController()
      paperPnlAbortRef.current = controller

      try {
        const snapshot = await api.getPaperPnlSnapshot({ signal: controller.signal })
        if (!active) return
        setPaperPnlSnapshot(snapshot)
        const snapshotTsMs = snapshot.ts_ms
        const totalPnlCents = snapshot.total_pnl_cents
        if (snapshotTsMs != null && totalPnlCents != null && snapshot.unrealized_pnl_status !== 'legacy_unverified') {
          setPaperPnlHistory((prev) => {
            if (prev.length > 0 && prev[prev.length - 1].tsMs === snapshotTsMs) {
              return prev
            }
            return [
              ...prev,
              {
                tsMs: snapshotTsMs,
                totalPnlCents,
              },
            ].slice(-MAX_PNL_POINTS)
          })
        }
      } catch (err) {
        if (!controller.signal.aborted && active) {
          const message = err instanceof Error ? err.message : 'Failed to fetch paper PnL snapshot'
          setApiError(message)
        }
      } finally {
        paperPnlAbortRef.current = null
        paperPnlInFlightRef.current = false
      }
    }

    const pollPaperIntents = async () => {
      if (paperIntentsInFlightRef.current) return
      paperIntentsInFlightRef.current = true
      paperIntentsAbortRef.current?.abort()
      const controller = new AbortController()
      paperIntentsAbortRef.current = controller

      try {
        const response = await api.getPaperIntents('OPEN', 200, { signal: controller.signal })
        if (!active) return
        setPaperOpenIntents(response.intents)
      } catch (err) {
        if (!controller.signal.aborted && active) {
          const message = err instanceof Error ? err.message : 'Failed to fetch paper intents'
          setApiError(message)
        }
      } finally {
        paperIntentsAbortRef.current = null
        paperIntentsInFlightRef.current = false
      }
    }

    const pollPaperFills = async () => {
      if (paperFillsInFlightRef.current) return
      paperFillsInFlightRef.current = true
      paperFillsAbortRef.current?.abort()
      const controller = new AbortController()
      paperFillsAbortRef.current = controller

      try {
        const response = await api.getPaperFills(200, { signal: controller.signal })
        if (!active) return
        setPaperFills(response.fills)
      } catch (err) {
        if (!controller.signal.aborted && active) {
          const message = err instanceof Error ? err.message : 'Failed to fetch paper fills'
          setApiError(message)
        }
      } finally {
        paperFillsAbortRef.current = null
        paperFillsInFlightRef.current = false
      }
    }

    const pollPaperPositions = async () => {
      if (paperPositionsInFlightRef.current) return
      paperPositionsInFlightRef.current = true
      paperPositionsAbortRef.current?.abort()
      const controller = new AbortController()
      paperPositionsAbortRef.current = controller

      try {
        const response = await api.getPaperPositions({ signal: controller.signal })
        if (!active) return
        setPaperPositions(response.positions)
      } catch (err) {
        if (!controller.signal.aborted && active) {
          const message = err instanceof Error ? err.message : 'Failed to fetch paper positions'
          setApiError(message)
        }
      } finally {
        paperPositionsAbortRef.current = null
        paperPositionsInFlightRef.current = false
      }
    }

    const pollPaperConfig = async () => {
      if (paperConfigInFlightRef.current) return
      paperConfigInFlightRef.current = true
      paperConfigAbortRef.current?.abort()
      const controller = new AbortController()
      paperConfigAbortRef.current = controller

      try {
        const config = await api.getPaperConfig({ signal: controller.signal })
        if (!active) return
        setPaperConfig(config)
      } catch (err) {
        if (!controller.signal.aborted && active) {
          const message = err instanceof Error ? err.message : 'Failed to fetch paper config'
          setApiError(message)
        }
      } finally {
        paperConfigAbortRef.current = null
        paperConfigInFlightRef.current = false
      }
    }

    const pollPaperMetrics = async () => {
      if (paperMetricsInFlightRef.current) return
      paperMetricsInFlightRef.current = true
      paperMetricsAbortRef.current?.abort()
      const controller = new AbortController()
      paperMetricsAbortRef.current = controller

      try {
        const metrics = await api.getPaperMetrics({ signal: controller.signal })
        if (!active) return
        setPaperMetrics(metrics)
      } catch (err) {
        if (!controller.signal.aborted && active) {
          const message = err instanceof Error ? err.message : 'Failed to fetch paper metrics'
          setApiError(message)
        }
      } finally {
        paperMetricsAbortRef.current = null
        paperMetricsInFlightRef.current = false
      }
    }

    pollPaperSummary()
    pollPaperPnlSnapshot()
    pollPaperIntents()
    pollPaperFills()
    pollPaperPositions()
    pollPaperConfig()
    pollPaperMetrics()

    paperSummaryIntervalRef.current = setInterval(pollPaperSummary, 5000)
    paperPnlIntervalRef.current = setInterval(pollPaperPnlSnapshot, 5000)
    paperIntentsIntervalRef.current = setInterval(pollPaperIntents, 2000)
    paperFillsIntervalRef.current = setInterval(pollPaperFills, 5000)
    paperPositionsIntervalRef.current = setInterval(pollPaperPositions, 5000)
    paperConfigIntervalRef.current = setInterval(pollPaperConfig, 5000)
    paperMetricsIntervalRef.current = setInterval(pollPaperMetrics, 5000)

    return () => {
      active = false
      clearPaperPolling()
    }
  }, [paperTradingState, clearPaperPolling])

  useEffect(() => {
    if (!selectedIntentId || paperTradingState !== 'running') {
      paperDebugAbortRef.current?.abort()
      paperDebugAbortRef.current = null
      setPaperIntentDebug(null)
      setPaperDebugLoading(false)
      return
    }

    paperDebugAbortRef.current?.abort()
    const controller = new AbortController()
    paperDebugAbortRef.current = controller
    setPaperDebugLoading(true)

    api.getPaperIntentDebug(selectedIntentId, { signal: controller.signal })
      .then((payload) => {
        if (!controller.signal.aborted) {
          setPaperIntentDebug(payload)
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          const message = err instanceof Error ? err.message : 'Failed to fetch paper intent debug'
          setApiError(message)
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setPaperDebugLoading(false)
        }
      })

    return () => {
      controller.abort()
    }
  }, [selectedIntentId, paperTradingState])

  const handleStartScanner = useCallback(async () => {
    if (scannerState === 'running' || scannerState === 'starting' || scannerState === 'stopping') {
      return
    }
    await startScannerRequest()
  }, [scannerState, startScannerRequest])

  const handleStopScanner = useCallback(async () => {
    if (scannerState === 'stopping') return

    clearScannerPolling()
    setScannerState('stopping')
    setApiError(null)

    try {
      const response = await api.stopScanner()
      if (response.reason === 'not_running') {
        setScannerInfoMessage('Scanner was not running.')
      }
      setScannerState('stopped')
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to stop scanner'
      setApiError(message)
      setScannerState('error')
    }
  }, [scannerState, clearScannerPolling])

  const handleStartPaperTrading = useCallback(async () => {
    if (paperTradingState === 'running' || paperTradingState === 'starting' || paperTradingState === 'stopping') {
      return
    }
    setPaperTradingState('starting')
    setApiError(null)

    try {
      const response = await api.startPaperTrading()
      if (response.reason === 'not_available') {
        setApiError('Paper trading not available (not enabled or not initialized)')
        setPaperTradingState('stopped')
        return
      }
      setPaperTradingState('running')
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to start paper trading'
      setApiError(message)
      setPaperTradingState('error')
    }
  }, [paperTradingState])

  const handleStopPaperTrading = useCallback(async () => {
    if (paperTradingState === 'stopping') return

    clearPaperPolling()
    setSelectedIntentId(null)
    setPaperTradingState('stopping')
    setApiError(null)

    try {
      await api.stopPaperTrading()
      setPaperTradingState('stopped')
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to stop paper trading'
      setApiError(message)
      setPaperTradingState('error')
    }
  }, [paperTradingState, clearPaperPolling])

  const handleRowClick = useCallback((signal: SignalResponse) => {
    setSelectedTicker(signal.ticker)
    setSelectedSignal(signal)
    setDrawerOpen(true)
  }, [])

  const selectedSeries = selectedTicker ? chartByTicker[selectedTicker] ?? [] : []
  const sortedPaperFills = [...paperFills].sort((a, b) => {
    const delta = a.fill_ts_ms - b.fill_ts_ms
    return paperFillSortDirection === 'desc' ? -delta : delta
  })
  const discoveredUniverseEntries = Object.entries(scannerStatus?.discovered_universe_counts ?? {})
  const summaryIntentsByUniverse = Object.entries(
    paperSummary?.entry_intents_created_by_universe ?? {}
  )
  const summaryFillsByUniverse = Object.entries(
    paperSummary?.entry_fills_by_universe ?? {}
  )
  const showSummaryUniverseBreakdown =
    summaryIntentsByUniverse.length > 0 || summaryFillsByUniverse.length > 0
  const debugIntentUniverse =
    paperIntentDebug?.intent &&
    typeof paperIntentDebug.intent === 'object' &&
    'universe' in paperIntentDebug.intent
      ? (paperIntentDebug.intent.universe as string | null | undefined)
      : undefined

  const paperTabs: { key: PaperTab; label: string }[] = [
    { key: 'summary', label: 'Summary' },
    { key: 'intents', label: 'Intents' },
    { key: 'fills', label: 'Fills' },
    { key: 'positions', label: 'Positions' },
    { key: 'config', label: 'Config' },
  ]

  const handleIntentDebugOpen = (intentId: string) => {
    setSelectedIntentId(intentId)
  }

  const handleFillIntentDebugOpen = (intentId: string) => {
    setActivePaperTab('intents')
    setSelectedIntentId(intentId)
  }

  return (
    <div className="h-screen w-screen bg-zinc-950 text-zinc-100 flex flex-col font-mono">
      <div className="bg-zinc-900/60 border-b border-white/10 px-6 py-4">
        <h1 className="text-2xl font-bold text-zinc-100">Kalshi Edge Bot Dashboard</h1>
        <p className="text-xs text-zinc-400 mt-1">
          Automated market scanning, paper trading, and observability
        </p>
      </div>

      <StatusBanner isConnected={isConnected} lastHeartbeat={lastHeartbeat} />
      <APIErrorBanner show={!!(healthError || apiError)} message={healthError || apiError || ''} />

      <div className="flex-1 flex flex-col overflow-y-auto">
        <div className="border-b border-white/10 bg-zinc-900/60 p-6 space-y-4">
          <div>
            <h2 className="text-lg font-semibold text-zinc-100">Automated Scanner Engine</h2>
            <p className="text-xs text-zinc-400 mt-1">
              Internal bot scanner status every 2s and top-signal snapshot every 10s
            </p>
          </div>

          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="text-xs text-zinc-300">
              State:{' '}
              <span className="font-semibold text-zinc-100 uppercase tracking-wide">
                {scannerState}
              </span>
            </div>

            <div className="flex gap-2">
              {(scannerState === 'stopped' || scannerState === 'error') && (
                <button
                  onClick={handleStartScanner}
                  className="px-4 py-2 bg-emerald-700 hover:bg-emerald-600 border border-emerald-500/30 rounded text-sm text-white transition"
                >
                  Start Scanner
                </button>
              )}

              {(scannerState === 'running' || scannerState === 'starting' || scannerState === 'stopping') && (
                <button
                  onClick={handleStopScanner}
                  disabled={scannerState === 'stopping'}
                  className="px-4 py-2 bg-red-900/50 hover:bg-red-900/70 disabled:bg-zinc-700 border border-red-700/40 rounded text-sm text-red-100 transition"
                >
                  {scannerState === 'stopping' ? 'Stopping...' : 'Stop Scanner'}
                </button>
              )}
            </div>
          </div>

          <ScannerStatusStrip status={scannerStatus} />

          <div className="text-xs text-zinc-400 flex flex-wrap items-center gap-3">
            <span>
              Enabled universes:{' '}
              <span className="text-zinc-200">
                {scannerStatus?.enabled_universes?.length
                  ? scannerStatus.enabled_universes.join(', ')
                  : '—'}
              </span>
            </span>
            <span className="text-zinc-500">|</span>
            <span className="flex items-center gap-1.5">
              <span>Discovered:</span>
              {discoveredUniverseEntries.length > 0 ? (
                discoveredUniverseEntries.map(([universe, count]) => (
                  <span key={universe} className="px-1.5 py-0.5 rounded border border-white/10 bg-zinc-900/50 text-zinc-300 tabular-nums">
                    {formatUniverse(universe)} {count}
                  </span>
                ))
              ) : (
                <span className="text-zinc-200">—</span>
              )}
            </span>
          </div>

          {scannerInfoMessage && (
            <p className="text-xs text-amber-300">{scannerInfoMessage}</p>
          )}
        </div>

        <div className="border-b border-white/10 bg-zinc-900/40 p-6 space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-zinc-100">Paper Trading</h3>
            <p className="text-xs text-zinc-400 mt-1">
              Start/stop automated paper execution simulation and lifecycle tracking
            </p>
          </div>

          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="text-xs text-zinc-300">
              State:{' '}
              <span className="font-semibold text-zinc-100 uppercase tracking-wide">
                {paperTradingState}
              </span>
            </div>

            <div className="flex gap-2">
              {(paperTradingState === 'stopped' || paperTradingState === 'error') && (
                <button
                  onClick={handleStartPaperTrading}
                  className="px-4 py-2 bg-blue-700 hover:bg-blue-600 border border-blue-500/30 rounded text-sm text-white transition"
                >
                  Start Paper Trading
                </button>
              )}

              {(paperTradingState === 'running' || paperTradingState === 'starting' || paperTradingState === 'stopping') && (
                <button
                  onClick={handleStopPaperTrading}
                  disabled={paperTradingState === 'stopping'}
                  className="px-4 py-2 bg-red-900/50 hover:bg-red-900/70 disabled:bg-zinc-700 border border-red-700/40 rounded text-sm text-red-100 transition"
                >
                  {paperTradingState === 'stopping' ? 'Stopping...' : 'Stop Paper Trading'}
                </button>
              )}
            </div>
          </div>
        </div>

        <div className="border-b border-white/10 bg-zinc-900/30 p-6 space-y-4">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <h3 className="text-sm font-semibold text-zinc-100">Paper Telemetry</h3>
              <p className="text-xs text-zinc-400 mt-1">Live paper observability while the paper engine is running</p>
            </div>
            {paperMetrics && (
              <div className="text-xs text-zinc-300 flex items-center gap-4">
                <span>Fill rate: <span className="tabular-nums text-zinc-100">{(paperMetrics.fill_rate * 100).toFixed(1)}%</span></span>
                <span>Avg fill: <span className="tabular-nums text-zinc-100">{paperMetrics.avg_time_to_fill_seconds == null ? '—' : `${paperMetrics.avg_time_to_fill_seconds.toFixed(1)}s`}</span></span>
                <span>Fills 1h: <span className="tabular-nums text-zinc-100">{paperMetrics.fills_last_1h}</span></span>
                <span>Intents 1h: <span className="tabular-nums text-zinc-100">{paperMetrics.intents_last_1h}</span></span>
              </div>
            )}
          </div>

          <div className="flex gap-2 flex-wrap">
            {paperTabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setActivePaperTab(tab.key)}
                className={`px-3 py-1.5 rounded text-xs border transition ${
                  activePaperTab === tab.key
                    ? 'bg-zinc-200 text-zinc-900 border-zinc-100'
                    : 'bg-zinc-800 text-zinc-100 border-white/10 hover:bg-zinc-700'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {activePaperTab === 'summary' && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 text-xs">
                <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                  <p className="text-zinc-500">Total Intents</p>
                  <p className="text-zinc-100 tabular-nums mt-1">{formatMetric(paperSummary?.total_intents_created)}</p>
                </div>
                <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                  <p className="text-zinc-500">Total Fills</p>
                  <p className="text-zinc-100 tabular-nums mt-1">{formatMetric(paperSummary?.total_fills)}</p>
                </div>
                <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                  <p className="text-zinc-500">Realized PnL</p>
                  <p className="text-zinc-100 tabular-nums mt-1">{formatCurrencyCents(paperSummary?.realized_pnl_cents)}</p>
                </div>
                <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                  <p className="text-zinc-500">Unrealized PnL</p>
                  <p className="text-zinc-100 tabular-nums mt-1">{formatCurrencyCents(paperSummary?.unrealized_pnl_cents)}</p>
                </div>
                <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                  <p className="text-zinc-500">Total PnL</p>
                  <p className="text-zinc-100 tabular-nums mt-1">{formatCurrencyCents(paperSummary?.total_pnl_cents)}</p>
                </div>
                <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                  <p className="text-zinc-500">Open Pos / Orders</p>
                  <p className="text-zinc-100 tabular-nums mt-1">
                    {formatMetric(paperPnlSnapshot?.open_positions_count)} / {formatMetric(paperPnlSnapshot?.open_orders_count)}
                  </p>
                </div>
              </div>

              {(paperSummary?.unrealized_unmarked_positions_count ?? 0) > 0 && (
                <p className="text-xs text-amber-300">
                  {paperSummary?.unrealized_unmarked_positions_count} positions unmarked
                  ({paperSummary?.unmarked_contracts} contracts, {formatCurrencyCents(paperSummary?.unmarked_cost_basis_cents)} cost).
                  {' '}Marked positions subtotal {formatCurrencyCents(paperSummary?.marked_unrealized_pnl_cents)}.
                </p>
              )}

              <MiniPnlChart points={paperPnlHistory} />

              {showSummaryUniverseBreakdown && (
                <div className="border border-white/10 rounded bg-zinc-900/40 p-3 text-xs space-y-2">
                  <p className="text-zinc-400">Universe breakdown</p>
                  {summaryIntentsByUniverse.length > 0 && (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-zinc-500">Entry intents:</span>
                      {summaryIntentsByUniverse.map(([universe, count]) => (
                        <span key={`intent-${universe}`} className="px-1.5 py-0.5 rounded border border-white/10 bg-zinc-900/50 text-zinc-300 tabular-nums">
                          {formatUniverse(universe)} {count}
                        </span>
                      ))}
                    </div>
                  )}
                  {summaryFillsByUniverse.length > 0 && (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-zinc-500">Entry fills:</span>
                      {summaryFillsByUniverse.map(([universe, count]) => (
                        <span key={`fill-${universe}`} className="px-1.5 py-0.5 rounded border border-white/10 bg-zinc-900/50 text-zinc-300 tabular-nums">
                          {formatUniverse(universe)} {count}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              <div className="border border-white/10 rounded bg-zinc-900/40 overflow-hidden">
                <div className="px-3 py-2 border-b border-white/10 text-xs text-zinc-400">Recent fills</div>
                <div className="max-h-56 overflow-auto">
                  <table className="w-full text-xs">
                    <thead className="bg-zinc-900 sticky top-0">
                      <tr className="text-zinc-500">
                        <th className="text-left px-3 py-2">Time</th>
                        <th className="text-left px-3 py-2">Ticker</th>
                        <th className="text-left px-3 py-2">Universe</th>
                        <th className="text-left px-3 py-2">Side</th>
                        <th className="text-left px-3 py-2">Price</th>
                        <th className="text-left px-3 py-2">Size</th>
                        <th className="text-left px-3 py-2">Intent</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sortedPaperFills.slice(0, 20).map((fill) => (
                        <tr key={fill.id} className="border-t border-white/5 hover:bg-white/5">
                          <td className="px-3 py-2 tabular-nums">{formatTimestamp(fill.fill_ts_ms)}</td>
                          <td className="px-3 py-2">{fill.ticker}</td>
                          <td className="px-3 py-2"><UniverseBadge universe={fill.universe} /></td>
                          <td className="px-3 py-2">{fill.side}</td>
                          <td className="px-3 py-2 tabular-nums">{fill.fill_price_cents.toFixed(2)}</td>
                          <td className="px-3 py-2 tabular-nums">{fill.fill_size}</td>
                          <td className="px-3 py-2">
                            <button
                              onClick={() => handleFillIntentDebugOpen(fill.intent_id)}
                              className="text-blue-300 hover:text-blue-200"
                            >
                              {fill.intent_id}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {activePaperTab === 'intents' && (
            <div className="border border-white/10 rounded bg-zinc-900/40 overflow-hidden">
              <div className="px-3 py-2 border-b border-white/10 text-xs text-zinc-400">Open intents</div>
              <div className="max-h-64 overflow-auto">
                <table className="w-full text-xs">
                  <thead className="bg-zinc-900 sticky top-0">
                    <tr className="text-zinc-500">
                      <th className="text-left px-3 py-2">Ticker</th>
                        <th className="text-left px-3 py-2">Universe</th>
                      <th className="text-left px-3 py-2">Side</th>
                      <th className="text-left px-3 py-2">Price</th>
                      <th className="text-left px-3 py-2">Queue Ahead</th>
                      <th className="text-left px-3 py-2">Age</th>
                      <th className="text-left px-3 py-2">Status</th>
                      <th className="text-left px-3 py-2">Intent</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paperOpenIntents.map((intent) => (
                      <tr
                        key={intent.id}
                        className="border-t border-white/5 hover:bg-white/5 cursor-pointer"
                        onClick={() => handleIntentDebugOpen(intent.id)}
                      >
                        <td className="px-3 py-2">{intent.ticker}</td>
                        <td className="px-3 py-2"><UniverseBadge universe={intent.universe} /></td>
                        <td className="px-3 py-2">{intent.side}</td>
                        <td className="px-3 py-2 tabular-nums">{intent.quote_price_cents.toFixed(2)}</td>
                        <td className="px-3 py-2 tabular-nums">{formatMetric(intent.queue_ahead_contracts)}</td>
                        <td className="px-3 py-2 tabular-nums">{formatAgeMs(intent.created_ts_ms)}</td>
                        <td className="px-3 py-2">{intent.status}</td>
                        <td className="px-3 py-2 text-blue-300">{intent.id}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {activePaperTab === 'fills' && (
            <div className="border border-white/10 rounded bg-zinc-900/40 overflow-hidden">
              <div className="px-3 py-2 border-b border-white/10 text-xs text-zinc-400 flex items-center justify-between">
                <span>Recent fills</span>
                <button
                  onClick={() =>
                    setPaperFillSortDirection((prev) => (prev === 'desc' ? 'asc' : 'desc'))
                  }
                  className="text-zinc-300 hover:text-zinc-100"
                >
                  Time: {paperFillSortDirection.toUpperCase()}
                </button>
              </div>
              <div className="max-h-64 overflow-auto">
                <table className="w-full text-xs">
                  <thead className="bg-zinc-900 sticky top-0">
                    <tr className="text-zinc-500">
                      <th className="text-left px-3 py-2">Time</th>
                      <th className="text-left px-3 py-2">Ticker</th>
                      <th className="text-left px-3 py-2">Universe</th>
                      <th className="text-left px-3 py-2">Side</th>
                      <th className="text-left px-3 py-2">Price</th>
                      <th className="text-left px-3 py-2">Size</th>
                      <th className="text-left px-3 py-2">Queue@Fill</th>
                      <th className="text-left px-3 py-2">Intent</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedPaperFills.map((fill) => (
                      <tr key={fill.id} className="border-t border-white/5 hover:bg-white/5">
                        <td className="px-3 py-2 tabular-nums">{formatTimestamp(fill.fill_ts_ms)}</td>
                        <td className="px-3 py-2">{fill.ticker}</td>
                        <td className="px-3 py-2"><UniverseBadge universe={fill.universe} /></td>
                        <td className="px-3 py-2">{fill.side}</td>
                        <td className="px-3 py-2 tabular-nums">{fill.fill_price_cents.toFixed(2)}</td>
                        <td className="px-3 py-2 tabular-nums">{fill.fill_size}</td>
                        <td className="px-3 py-2 tabular-nums">{formatMetric(fill.queue_ahead_at_fill)}</td>
                        <td className="px-3 py-2">
                          <button
                            onClick={() => handleFillIntentDebugOpen(fill.intent_id)}
                            className="text-blue-300 hover:text-blue-200"
                          >
                            {fill.intent_id}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {activePaperTab === 'positions' && (
            <div className="border border-white/10 rounded bg-zinc-900/40 overflow-hidden">
              <div className="px-3 py-2 border-b border-white/10 text-xs text-zinc-400">Positions</div>
              <div className="max-h-64 overflow-auto">
                <table className="w-full text-xs">
                  <thead className="bg-zinc-900 sticky top-0">
                    <tr className="text-zinc-500">
                      <th className="text-left px-3 py-2">Ticker</th>
                      <th className="text-left px-3 py-2">Universe</th>
                      <th className="text-left px-3 py-2">Side</th>
                      <th className="text-left px-3 py-2">Net Contracts</th>
                      <th className="text-left px-3 py-2">Avg Entry</th>
                      <th className="text-left px-3 py-2">Realized PnL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paperPositions.map((position) => (
                      <tr key={`${position.ticker}-${position.side}`} className="border-t border-white/5">
                        <td className="px-3 py-2">{position.ticker}</td>
                        <td className="px-3 py-2"><UniverseBadge universe={position.universe} /></td>
                        <td className="px-3 py-2">{position.side}</td>
                        <td className="px-3 py-2 tabular-nums">{position.net_contracts}</td>
                        <td className="px-3 py-2 tabular-nums">{position.avg_entry_price_cents.toFixed(2)}</td>
                        <td className="px-3 py-2 tabular-nums">{formatCurrencyCents(position.realized_pnl_cents)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {activePaperTab === 'config' && (
            <div className="space-y-3">
              {paperConfig && (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                  <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                    <p className="text-zinc-500">Min Edge</p>
                    <p className="text-zinc-100 tabular-nums mt-1">{paperConfig.min_edge_cents}</p>
                  </div>
                  <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                    <p className="text-zinc-500">Fill Horizon s</p>
                    <p className="text-zinc-100 tabular-nums mt-1">{paperConfig.fill_horizon_seconds}</p>
                  </div>
                  <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                    <p className="text-zinc-500">Max Open Orders</p>
                    <p className="text-zinc-100 tabular-nums mt-1">{paperConfig.max_open_orders}</p>
                  </div>
                  <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                    <p className="text-zinc-500">Max Pos / Ticker</p>
                    <p className="text-zinc-100 tabular-nums mt-1">{paperConfig.max_pos_per_ticker}</p>
                  </div>
                </div>
              )}

              <pre className="border border-white/10 rounded bg-zinc-900/40 p-3 text-xs text-zinc-300 overflow-auto max-h-72">
                {JSON.stringify(paperConfig, null, 2)}
              </pre>
            </div>
          )}
        </div>

        <SignalsTable
          signals={signals}
          isLoading={isSignalsLoading || scannerState === 'starting'}
          lastUpdated={lastUpdated}
          onRowClick={handleRowClick}
        />
      </div>

      <OrderbookDrawer
        signal={selectedSignal}
        chartPoints={selectedSeries}
        onClose={() => setDrawerOpen(false)}
        isOpen={drawerOpen}
      />

      {selectedIntentId && (
        <div className="fixed inset-0 z-40 bg-black/70 flex items-center justify-center p-6">
          <div className="w-full max-w-3xl max-h-[80vh] overflow-auto border border-white/10 rounded bg-zinc-950 shadow-2xl">
            <div className="px-4 py-3 border-b border-white/10 flex items-center justify-between">
              <div>
                <h4 className="text-sm font-semibold text-zinc-100">Paper Intent Debug</h4>
                <p className="text-xs text-zinc-400 mt-1">Intent {selectedIntentId}</p>
              </div>
              <button
                onClick={() => setSelectedIntentId(null)}
                className="px-3 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 border border-white/10 text-xs"
              >
                Close
              </button>
            </div>

            <div className="p-4 space-y-4 text-xs">
              {paperDebugLoading && (
                <p className="text-zinc-400">Loading diagnostics...</p>
              )}

              {paperIntentDebug && (
                <>
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                    <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                      <p className="text-zinc-500">Last Seen Best Bid</p>
                      <p className="text-zinc-100 tabular-nums mt-1">{formatMetric((paperIntentDebug.intent.last_seen_best_bid as number | null | undefined) ?? null)}</p>
                    </div>
                    <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                      <p className="text-zinc-500">Queue Ahead</p>
                      <p className="text-zinc-100 tabular-nums mt-1">{formatMetric((paperIntentDebug.intent.queue_ahead_contracts as number | null | undefined) ?? null)}</p>
                    </div>
                    <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                      <p className="text-zinc-500">Best Bid</p>
                      <p className="text-zinc-100 tabular-nums mt-1">{formatMetric(paperIntentDebug.simulator.best_bid)}</p>
                    </div>
                    <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                      <p className="text-zinc-500">Fill Horizon s</p>
                      <p className="text-zinc-100 tabular-nums mt-1">{formatMetric((paperIntentDebug.intent.max_hold_seconds as number | null | undefined) ?? null)}</p>
                    </div>
                    <div className="border border-white/10 rounded p-3 bg-zinc-900/40">
                      <p className="text-zinc-500">Universe</p>
                      <p className="text-zinc-100 tabular-nums mt-1">{formatUniverse(debugIntentUniverse)}</p>
                    </div>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <div>
                      <p className="text-zinc-400 mb-1">Eligibility flags</p>
                      <pre className="border border-white/10 rounded bg-zinc-900/40 p-3 overflow-auto max-h-48 text-zinc-300">
                        {JSON.stringify(paperIntentDebug.simulator.eligibility, null, 2)}
                      </pre>
                    </div>
                    <div>
                      <p className="text-zinc-400 mb-1">Simulator diagnostics</p>
                      <pre className="border border-white/10 rounded bg-zinc-900/40 p-3 overflow-auto max-h-48 text-zinc-300">
                        {JSON.stringify(paperIntentDebug.simulator, null, 2)}
                      </pre>
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
