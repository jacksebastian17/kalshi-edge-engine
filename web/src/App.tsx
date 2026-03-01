import { useState, useEffect, useCallback, useRef } from 'react'
import { api, SignalResponse } from './lib/api'
import { WatchlistInput } from './components/WatchlistInput'
import { SignalsTable } from './components/SignalsTable'
import { OrderbookDrawer } from './components/OrderbookDrawer'
import { StatusBanner, APIErrorBanner, useHealthCheck } from './components/StatusBanner'

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
  const inFlightRef = useRef(false)
  const pollAbortRef = useRef<AbortController | null>(null)

  const { isConnected, lastHeartbeat, error: healthError } = useHealthCheck(5000)

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
      } catch (err) {
        if (pollController.signal.aborted) {
          return
        }
        const message =
          err instanceof Error ? err.message : 'Failed to fetch signals'
        setApiError(message)
      } finally {
        inFlightRef.current = false
        setIsLoading(false)
      }
    }

    pollSignals()
    const timer = setInterval(pollSignals, pollInterval)

    return () => {
      clearInterval(timer)
      pollAbortRef.current?.abort()
      inFlightRef.current = false
    }
  }, [currentTickers, pollInterval, isPaused, selectedTicker, drawerOpen])

  const handleScan = useCallback((tickers: string[]) => {
    setCurrentTickers(tickers)
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
