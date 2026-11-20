import { useEffect, useState } from 'react'
import { DebugOrderbookResponse, SignalResponse, api } from '../lib/api'

type EdgePoint = {
  t: number
  maxEdge: number
  netEdgeYes: number | null
  netEdgeNo: number | null
  gap: number | null
}

interface OrderbookDrawerProps {
  signal: SignalResponse | null
  chartPoints: EdgePoint[]
  onClose: () => void
  isOpen: boolean
}

interface OrderbookState {
  data: DebugOrderbookResponse | null
  isLoading: boolean
  error: string | null
}

function getMaxEdge(signal: SignalResponse | null): number {
  if (!signal) return 0
  const yes = signal.net_edge_yes ?? Number.NEGATIVE_INFINITY
  const no = signal.net_edge_no ?? Number.NEGATIVE_INFINITY
  const max = Math.max(yes, no)
  return Number.isFinite(max) ? max : 0
}

function formatUniverse(universe?: string): string {
  const value = universe?.trim().toUpperCase()
  return value && value.length > 0 ? value : '—'
}

function MetricRow({
  label,
  value,
  valueClassName = 'text-zinc-200',
}: {
  label: string
  value: number | null | undefined
  valueClassName?: string
}) {
  return (
    <div className="flex justify-between">
      <span>{label}</span>
      <span className={valueClassName}>{value == null ? '—' : value.toFixed(2)}</span>
    </div>
  )
}

function MaxEdgeChart({ points }: { points: EdgePoint[] }) {
  if (points.length === 0) {
    return (
      <div className="h-40 rounded border border-dashed border-white/10 bg-zinc-950 flex items-center justify-center text-sm text-zinc-500">
        No data yet
      </div>
    )
  }

  const width = 560
  const height = 160
  const padX = 10
  const padY = 10
  const minY = Math.min(...points.map((point) => point.maxEdge))
  const maxY = Math.max(...points.map((point) => point.maxEdge))
  const ySpan = Math.max(maxY - minY, 0.01)
  const usableW = width - padX * 2
  const usableH = height - padY * 2

  const path = points
    .map((point, index) => {
      const x = padX + (index / Math.max(points.length - 1, 1)) * usableW
      const y = padY + (1 - (point.maxEdge - minY) / ySpan) * usableH
      return `${index === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`
    })
    .join(' ')

  const last = points[points.length - 1]

  return (
    <div className="space-y-2">
      <div className="h-40 rounded border border-white/10 bg-zinc-950 p-2">
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-full">
          <line
            x1={padX}
            y1={height - padY}
            x2={width - padX}
            y2={height - padY}
            stroke="rgba(255,255,255,0.12)"
            strokeWidth="1"
          />
          <path
            d={path}
            fill="none"
            stroke="#34d399"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      <div className="flex items-center justify-between text-[11px] text-zinc-500 tabular-nums">
        <span>Min {minY.toFixed(2)}¢</span>
        <span>Latest {last.maxEdge.toFixed(2)}¢</span>
        <span>Max {maxY.toFixed(2)}¢</span>
      </div>
    </div>
  )
}

export function OrderbookDrawer({
  signal,
  chartPoints,
  onClose,
  isOpen,
}: OrderbookDrawerProps) {
  const [orderbook, setOrderbook] = useState<OrderbookState>({
    data: null,
    isLoading: false,
    error: null,
  })
  const [showRawJson, setShowRawJson] = useState(false)

  useEffect(() => {
    if (!isOpen || !signal) return

    const orderbookController = new AbortController()

    const fetchOrderbook = async () => {
      setOrderbook({ data: null, isLoading: true, error: null })
      try {
        const data = await api.debugOrderbook(signal.ticker, {
          signal: orderbookController.signal,
        })
        setOrderbook({ data, isLoading: false, error: null })
      } catch (err) {
        if (orderbookController.signal.aborted) {
          return
        }
        setOrderbook({
          data: null,
          isLoading: false,
          error: err instanceof Error ? err.message : 'Failed to load orderbook',
        })
      }
    }

    fetchOrderbook()

    return () => {
      orderbookController.abort()
    }
  }, [signal, isOpen])

  if (!isOpen) return null

  const maxEdge = getMaxEdge(signal)

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/80 cursor-pointer" onClick={onClose} />

      <div className="w-[34rem] max-w-[90vw] bg-zinc-900/95 border-l border-white/10 flex flex-col overflow-hidden">
        <div className="flex items-center justify-between border-b border-white/10 px-6 py-4 bg-zinc-900">
          <div>
            <h2 className="text-lg font-bold text-zinc-100">{signal?.ticker}</h2>
            <p className="text-xs text-zinc-400 mt-1">
              Reason: {signal?.reason_code} · Universe: {formatUniverse(signal?.universe)}
            </p>
          </div>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300 text-xl">
            ✕
          </button>
        </div>

        <div className="flex-1 overflow-auto p-6 space-y-4">
          <div className="rounded border border-white/10 bg-zinc-900 p-4">
            <p className="text-xs text-zinc-400 mb-2">Best Spread Room After Costs</p>
            <p
              className={`text-4xl font-semibold tabular-nums ${
                maxEdge >= 0 ? 'text-emerald-300' : 'text-red-300'
              }`}
            >
              {maxEdge >= 0 ? '+' : ''}
              {maxEdge.toFixed(2)}¢
            </p>
            <p className="text-xs text-zinc-400 mt-2 tabular-nums">
              {signal?.reason_code} · LQ {signal?.top_liquidity ?? 0}
            </p>
          </div>

          <div className="rounded border border-white/10 bg-zinc-900 p-4">
            <h3 className="text-sm font-bold text-zinc-200 mb-3">Spread Room Over Time</h3>
            <MaxEdgeChart points={chartPoints} />
          </div>

          <div className="rounded border border-white/10 bg-zinc-900 p-4">
            <h3 className="text-sm font-bold text-zinc-200 mb-3">Signal Metrics</h3>
            <div className="space-y-1 text-xs text-zinc-400 tabular-nums">
              <MetricRow
                label="YES Spread Room After Costs"
                value={signal?.net_edge_yes}
                valueClassName="text-emerald-300"
              />
              <MetricRow
                label="NO Spread Room After Costs"
                value={signal?.net_edge_no}
                valueClassName="text-sky-300"
              />
              <MetricRow label="Quoted Spread" value={signal?.inside_width_yes ?? signal?.inside_width_no} />
              <div className="flex justify-between">
                <span>Top Liquidity</span>
                <span>{signal?.top_liquidity ?? '—'}</span>
              </div>
              <div className="flex justify-between">
                <span>Universe</span>
                <span>{formatUniverse(signal?.universe)}</span>
              </div>
              <div className="flex justify-between">
                <span>Timestamp</span>
                <span>
                  {signal?.timestamp_ms
                    ? new Date(signal.timestamp_ms).toLocaleTimeString()
                    : '—'}
                </span>
              </div>
            </div>
          </div>

          <div className="rounded border border-white/10 bg-zinc-900 p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-bold text-zinc-200">Raw Orderbook JSON</h3>
              <button
                onClick={() => setShowRawJson((prev) => !prev)}
                className="text-xs px-2 py-1 rounded border border-white/10 bg-zinc-800 hover:bg-zinc-700 text-zinc-200"
              >
                {showRawJson ? 'Hide' : 'Show'}
              </button>
            </div>

            {orderbook.isLoading && (
              <p className="text-xs text-zinc-500 animate-pulse">Loading orderbook...</p>
            )}

            {orderbook.error && <p className="text-xs text-red-400">{orderbook.error}</p>}

            {orderbook.data && showRawJson && (
              <pre className="text-[11px] leading-relaxed text-zinc-300 bg-zinc-950 border border-white/10 rounded p-3 overflow-auto max-h-72">
                {JSON.stringify(orderbook.data, null, 2)}
              </pre>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
