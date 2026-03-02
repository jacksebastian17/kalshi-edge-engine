import { useState, useEffect, useCallback, useRef } from 'react'
import { api, SignalResponse } from './lib/api'
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

function getMaxEdge(signal: SignalResponse): number {
  const yes = signal.net_edge_yes ?? Number.NEGATIVE_INFINITY
  const no = signal.net_edge_no ?? Number.NEGATIVE_INFINITY
  const max = Math.max(yes, no)
  return Number.isFinite(max) ? max : 0
}

export default function App() {
  const [signals, setSignals] = useState<SignalResponse[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [watchlist, setWatchlist] = useState('')
  const [pollInterval, setPollInterval] = useState(2000)
  const [isPaused, setIsPaused] = useState(false)
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null)
  const [selectedSignal, setSelectedSignal] = useState<SignalResponse | null>(
    null
  )
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [apiError, setApiError] = useState<string | null>(null)
  const [currentTickers, setCurrentTickers] = useState<string[]>([])
  const [chartByTicker, setChartByTicker] = useState<Record<string, EdgePoint[]>>(
    {}
  )
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const inFlightRef = useRef(false)
  const pollAbortRef = useRef<AbortController | null>(null)
  const hasRestoredRef = useRef(false)
  const [effectivePollInterval, setEffectivePollInterval] = useState(2000)
  const consecutiveErrorsRef = useRef(0)

  const { isConnected, lastHeartbeat, error: healthError } = useHealthCheck(5000)

  // Restore persisted state on mount
  useEffect(() => {
    if (hasRestoredRef.current) return
    hasRestoredRef.current = true

    const restored = loadPersistedState()
    setWatchlist(restored.watchlist)
    setPollInterval(restored.pollInterval)
    setIsPaused(restored.isPaused)
    setSelectedTicker(restored.selectedTicker)

    // Auto-start scanning if watchlist exists and not paused
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

  // Persist state changes
  useEffect(() => {
    if (!hasRestoredRef.current) return
    savePersistedState({
      watchlist,
      pollInterval,
      isPaused,
      selectedTicker,
    })
  }, [watchlist, pollInterval, isPaused, selectedTicker])

  // Polling loop
  useEffect(() => {
    if (isPaused || currentTickers.length === 0) {
      pollAbortRef.current?.abort()
      inFlightRef.current = false
      setIsLoading(false)
      return
    }

    const pollSignals = async () => {
      if (inFlightRef.current) {
        return
      }
      inFlightRef.current = true
      setIsLoading(true)
      pollAbortRef.current?.abort()
      const pollController = new AbortController()
      pollAbortRef.current = pollController

      try {
        const response = await api.scanSignals(currentTickers, {
          signal: pollController.signal,
        })
        setSignals(response)
        // Update lastUpdated timestamp to current frontend time
        setLastUpdated(Date.now())
        if (selectedTicker) {
          const selectedFromResponse = response.find(
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
        setApiError(null)
        // Reset backoff on success
        consecutiveErrorsRef.current = 0
        setEffectivePollInterval(pollInterval)
      } catch (err) {
        if (pollController.signal.aborted) {
          return
        }
        const message =
          err instanceof Error ? err.message : 'Failed to fetch signals'
        setApiError(message)
        
        // Implement exponential backoff: 2s -> 5s -> 10s (max)
        consecutiveErrorsRef.current += 1
        const backoffIntervals = [2000, 5000, 10000]
        const idx = Math.min(consecutiveErrorsRef.current - 1, backoffIntervals.length - 1)
        const nextInterval = backoffIntervals[idx]
        setEffectivePollInterval(nextInterval)
      } finally {
        inFlightRef.current = false
        setIsLoading(false)
      }
    }

    pollSignals()
    const timer = setInterval(pollSignals, effectivePollInterval)

    return () => {
      clearInterval(timer)
      pollAbortRef.current?.abort()
      inFlightRef.current = false
    }
  }, [currentTickers, pollInterval, effectivePollInterval, isPaused, selectedTicker, drawerOpen])

  // Reset backoff when user changes settings or tickers
  useEffect(() => {
    consecutiveErrorsRef.current = 0
    setEffectivePollInterval(pollInterval)
  }, [currentTickers, pollInterval])

  const handleScan = useCallback((tickers: string[], watchlistStr: string) => {
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

  const handleRowClick = (signal: SignalResponse) => {
    setSelectedTicker(signal.ticker)
    setSelectedSignal(signal)
    setDrawerOpen(true)
  }

  const selectedSeries = selectedTicker
    ? chartByTicker[selectedTicker] ?? []
    : []

  return (
    <div className="h-screen w-screen bg-zinc-950 text-zinc-100 flex flex-col font-mono">
      {/* Header */}
      <div className="bg-zinc-900/60 border-b border-white/10 px-6 py-4">
        <h1 className="text-2xl font-bold text-zinc-100">
          Kalshi Edge Dashboard
        </h1>
        <p className="text-xs text-zinc-400 mt-1">
          Real-time signal scanning & orderbook inspection
        </p>
      </div>

      {/* Status */}
      <StatusBanner isConnected={isConnected} lastHeartbeat={lastHeartbeat} />
      <APIErrorBanner
        show={!!(healthError || apiError)}
        message={healthError || apiError || ''}
      />

      {/* Main content */}
      <div className="flex-1 flex flex-col overflow-hidden">
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

        <SignalsTable
          signals={signals}
          isLoading={isLoading}
          lastUpdated={lastUpdated}
          onRowClick={handleRowClick}
        />
      </div>

      {/* Drawer */}
      <OrderbookDrawer
        signal={selectedSignal}
        chartPoints={selectedSeries}
        onClose={() => setDrawerOpen(false)}
        isOpen={drawerOpen}
      />
    </div>
  )
}
