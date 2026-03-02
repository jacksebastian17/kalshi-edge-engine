import { useState, useEffect, useCallback, useRef } from 'react'
import { api, ScannerStatusResponse, SignalResponse } from './lib/api'
import { WatchlistInput } from './components/WatchlistInput'
import { SignalsTable } from './components/SignalsTable'
import { OrderbookDrawer } from './components/OrderbookDrawer'
import { StatusBanner, APIErrorBanner, useHealthCheck } from './components/StatusBanner'
import { loadPersistedState, savePersistedState } from './lib/localStorage'

const MAX_CHART_POINTS = 300

type EdgePoint = {
  t: number
  maxEdge: number
  netEdgeYes: number | null
  netEdgeNo: number | null
  gap: number | null
}

type UIMode = 'landing' | 'scanner' | 'manual'
type ScannerState = 'stopped' | 'starting' | 'running' | 'stopping' | 'error'
type PaperTradingState = 'stopped' | 'starting' | 'running' | 'stopping' | 'error'

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

function failedCount(value: ScannerStatusResponse['tickers_failed_last_cycle'] | null | undefined): number | null {
  if (Array.isArray(value)) {
    return value.length
  }
  if (typeof value === 'number') {
    return value
  }
  return null
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

export default function App() {
  const [signals, setSignals] = useState<SignalResponse[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [watchlist, setWatchlist] = useState('')
  const [pollInterval, setPollInterval] = useState(2000)
  const [isPaused, setIsPaused] = useState(false)
  const [uiMode, setUiMode] = useState<UIMode>('landing')
  const [preferredMode, setPreferredMode] = useState<'scanner' | 'manual'>('scanner')
  const [scannerState, setScannerState] = useState<ScannerState>('stopped')
  const [paperTradingState, setPaperTradingState] = useState<PaperTradingState>('stopped')
  const [scannerStatus, setScannerStatus] = useState<ScannerStatusResponse | null>(null)
  const [scannerInfoMessage, setScannerInfoMessage] = useState<string | null>(null)
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null)
  const [selectedSignal, setSelectedSignal] = useState<SignalResponse | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [apiError, setApiError] = useState<string | null>(null)
  const [currentTickers, setCurrentTickers] = useState<string[]>([])
  const [chartByTicker, setChartByTicker] = useState<Record<string, EdgePoint[]>>({})
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [effectiveManualPollInterval, setEffectiveManualPollInterval] = useState(2000)

  const hasRestoredRef = useRef(false)
  const consecutiveErrorsRef = useRef(0)

  const manualInFlightRef = useRef(false)
  const statusInFlightRef = useRef(false)
  const topSignalsInFlightRef = useRef(false)

  const manualPollAbortRef = useRef<AbortController | null>(null)
  const statusPollAbortRef = useRef<AbortController | null>(null)
  const topSignalsPollAbortRef = useRef<AbortController | null>(null)

  const manualIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const statusIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const topSignalsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const { isConnected, lastHeartbeat, error: healthError } = useHealthCheck(5000)

  const clearManualPolling = useCallback(() => {
    if (manualIntervalRef.current) {
      clearInterval(manualIntervalRef.current)
      manualIntervalRef.current = null
    }
    manualPollAbortRef.current?.abort()
    manualPollAbortRef.current = null
    manualInFlightRef.current = false
    setIsLoading(false)
  }, [])

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
    setWatchlist(restored.watchlist)
    setPollInterval(restored.pollInterval)
    setIsPaused(restored.isPaused)
    setSelectedTicker(restored.selectedTicker)
    setPreferredMode(restored.preferredMode)
    setUiMode('landing')

    if (restored.watchlist.trim() && !restored.isPaused) {
      const tickers = restored.watchlist
        .split(',')
        .map((t) => t.trim().toUpperCase())
        .filter((t) => t.length > 0)

      if (tickers.length > 0) {
        setCurrentTickers(tickers)
      }
    }
  }, [])

  useEffect(() => {
    if (!hasRestoredRef.current) return
    savePersistedState({
      watchlist,
      pollInterval,
      isPaused,
      selectedTicker,
      preferredMode,
    })
  }, [watchlist, pollInterval, isPaused, selectedTicker, preferredMode])

  useEffect(() => {
    if (uiMode !== 'manual') {
      clearManualPolling()
      return
    }

    if (isPaused || currentTickers.length === 0) {
      clearManualPolling()
      return
    }

    const pollManualSignals = async () => {
      if (manualInFlightRef.current) {
        return
      }

      manualInFlightRef.current = true
      setIsLoading(true)
      manualPollAbortRef.current?.abort()
      const pollController = new AbortController()
      manualPollAbortRef.current = pollController

      try {
        const response = await api.scanSignals(currentTickers, {
          signal: pollController.signal,
        })
        updateSignals(response)
        setApiError(null)
        consecutiveErrorsRef.current = 0
        setEffectiveManualPollInterval(pollInterval)
      } catch (err) {
        if (pollController.signal.aborted) {
          return
        }

        const message = err instanceof Error ? err.message : 'Failed to fetch signals'
        setApiError(message)

        consecutiveErrorsRef.current += 1
        const backoffIntervals = [2000, 5000, 10000]
        const idx = Math.min(consecutiveErrorsRef.current - 1, backoffIntervals.length - 1)
        setEffectiveManualPollInterval(backoffIntervals[idx])
      } finally {
        manualInFlightRef.current = false
        setIsLoading(false)
      }
    }

    pollManualSignals()
    manualIntervalRef.current = setInterval(pollManualSignals, effectiveManualPollInterval)

    return () => {
      clearManualPolling()
    }
  }, [
    uiMode,
    isPaused,
    currentTickers,
    pollInterval,
    effectiveManualPollInterval,
    updateSignals,
    clearManualPolling,
  ])

  useEffect(() => {
    consecutiveErrorsRef.current = 0
    setEffectiveManualPollInterval(pollInterval)
  }, [currentTickers, pollInterval])

  useEffect(() => {
    if (uiMode !== 'scanner' || scannerState !== 'running') {
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
  }, [uiMode, scannerState, updateSignals, clearScannerPolling])

  const handleScan = useCallback((tickers: string[], watchlistStr: string) => {
    setPreferredMode('manual')
    setUiMode('manual')
    setIsPaused(false)
    setCurrentTickers(tickers)
    setWatchlist(watchlistStr)
    setSignals([])
    setApiError(null)

    if (selectedTicker && !tickers.includes(selectedTicker)) {
      setSelectedTicker(null)
      setSelectedSignal(null)
      setDrawerOpen(false)
    }
  }, [selectedTicker])

  const handleStartScanner = useCallback(async () => {
    setPreferredMode('scanner')
    setUiMode('scanner')
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
      if (response.reason === 'already_running') {
        // Already running, just show running state
      } else if (response.reason === 'not_available') {
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

    setPaperTradingState('stopping')
    setApiError(null)

    try {
      const response = await api.stopPaperTrading()
      if (response.reason === 'not_running') {
        // Not running, just show stopped state
      }
      setPaperTradingState('stopped')
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to stop paper trading'
      setApiError(message)
      setPaperTradingState('error')
    }
  }, [paperTradingState])

  const handleSwitchToManual = useCallback(() => {
    clearScannerPolling()
    setPreferredMode('manual')
    setUiMode('manual')
  }, [clearScannerPolling])

  const handleSwitchToScanner = useCallback(() => {
    clearManualPolling()
    setPreferredMode('scanner')
    setUiMode('scanner')
  }, [clearManualPolling])

  const handleRowClick = useCallback((signal: SignalResponse) => {
    setSelectedTicker(signal.ticker)
    setSelectedSignal(signal)
    setDrawerOpen(true)
  }, [])

  const selectedSeries = selectedTicker ? chartByTicker[selectedTicker] ?? [] : []

  return (
    <div className="h-screen w-screen bg-zinc-950 text-zinc-100 flex flex-col font-mono">
      <div className="bg-zinc-900/60 border-b border-white/10 px-6 py-4">
        <h1 className="text-2xl font-bold text-zinc-100">Kalshi Edge Dashboard</h1>
        <p className="text-xs text-zinc-400 mt-1">
          Real-time signal scanning & orderbook inspection
        </p>
      </div>

      <StatusBanner isConnected={isConnected} lastHeartbeat={lastHeartbeat} />
      <APIErrorBanner show={!!(healthError || apiError)} message={healthError || apiError || ''} />

      <div className="flex-1 flex flex-col overflow-hidden">
        {uiMode === 'landing' && (
          <div className="flex-1 flex items-center justify-center px-6">
            <div className="w-full max-w-2xl border border-white/10 bg-zinc-900/50 rounded p-8 space-y-6">
              <div>
                <h2 className="text-2xl font-semibold text-zinc-100">Mode Select</h2>
                <p className="text-sm text-zinc-400 mt-2">
                  Choose scanner mode for NBA market snapshots or manual watchlist mode.
                </p>
              </div>
              <div className="flex flex-col sm:flex-row gap-3">
                <button
                  onClick={handleStartScanner}
                  className={`px-4 py-3 rounded text-sm font-semibold transition ${
                    preferredMode === 'scanner'
                      ? 'bg-emerald-700 hover:bg-emerald-600 text-white'
                      : 'bg-zinc-800 hover:bg-zinc-700 border border-white/10 text-zinc-100'
                  }`}
                >
                  Start NBA Scanner
                </button>
                <button
                  onClick={handleSwitchToManual}
                  className={`px-4 py-3 rounded text-sm transition ${
                    preferredMode === 'manual'
                      ? 'bg-zinc-200 text-zinc-900 hover:bg-white'
                      : 'bg-zinc-800 hover:bg-zinc-700 border border-white/10 text-zinc-100'
                  }`}
                >
                  Manual Watchlist Mode
                </button>
              </div>
            </div>
          </div>
        )}

        {uiMode === 'scanner' && (
          <>
            <div className="border-b border-white/10 bg-zinc-900/60 p-6 space-y-4">
              <div>
                <h2 className="text-lg font-semibold text-zinc-100">NBA Scanner</h2>
                <p className="text-xs text-zinc-400 mt-1">
                  Polling scanner status every 2s and top signals every 10s
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
                      Start
                    </button>
                  )}

                  {(scannerState === 'running' || scannerState === 'starting' || scannerState === 'stopping') && (
                    <button
                      onClick={handleStopScanner}
                      disabled={scannerState === 'stopping'}
                      className="px-4 py-2 bg-red-900/50 hover:bg-red-900/70 disabled:bg-zinc-700 border border-red-700/40 rounded text-sm text-red-100 transition"
                    >
                      {scannerState === 'stopping' ? 'Stopping...' : 'Stop'}
                    </button>
                  )}

                  <button
                    onClick={handleSwitchToManual}
                    className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 border border-white/10 rounded text-sm text-zinc-100 transition"
                  >
                    Switch to Manual
                  </button>
                </div>
              </div>

              <ScannerStatusStrip status={scannerStatus} />

              {scannerInfoMessage && (
                <p className="text-xs text-amber-300">{scannerInfoMessage}</p>
              )}
            </div>

            <div className="border-b border-white/10 bg-zinc-900/40 p-6 space-y-3">
              <div>
                <h3 className="text-sm font-semibold text-zinc-100">Paper Trading</h3>
                <p className="text-xs text-zinc-400 mt-1">
                  Start/stop automated order simulation and filled order tracking
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

            <SignalsTable
              signals={signals}
              isLoading={isLoading || scannerState === 'starting'}
              lastUpdated={lastUpdated}
              onRowClick={handleRowClick}
            />
          </>
        )}

        {uiMode === 'manual' && (
          <>
            <WatchlistInput
              watchlist={watchlist}
              onWatchlistChange={setWatchlist}
              onScan={handleScan}
              isLoading={isLoading}
              pollInterval={pollInterval}
              onPollIntervalChange={setPollInterval}
              isPaused={isPaused}
              onPausedChange={setIsPaused}
            />

            <div className="border-b border-white/10 bg-zinc-900/40 px-6 py-3 flex items-center justify-end">
              <button
                onClick={handleSwitchToScanner}
                className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 border border-white/10 rounded text-sm text-zinc-100 transition"
              >
                Switch to Scanner
              </button>
            </div>

            <SignalsTable
              signals={signals}
              isLoading={isLoading}
              lastUpdated={lastUpdated}
              onRowClick={handleRowClick}
            />
          </>
        )}
      </div>

      <OrderbookDrawer
        signal={selectedSignal}
        chartPoints={selectedSeries}
        onClose={() => setDrawerOpen(false)}
        isOpen={drawerOpen}
      />
    </div>
  )
}
