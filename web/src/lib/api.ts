/**
 * Typed API client for Kalshi Edge Engine backend.
 */

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const DEFAULT_TIMEOUT_MS = 8000

export interface HealthResponse {
  status: string
}

export interface TopSignalsResponse {
  timestamp_ms: number
  signals: SignalResponse[]
  market_count: number
}

export interface ScannerStatusResponse {
  enabled: boolean
  last_scan_ms: number | null
  markets_discovered: number
  enabled_universes?: string[]
  discovered_universe_counts?: Record<string, number>
  markets_scanned_last_cycle: number
  last_cycle_duration_ms: number | null
  tickers_failed_last_cycle: string[] | number
  tickers_skipped_cooldown_last_cycle: string[] | number
  snapshot_timestamp_ms: number | null
  snapshot_age_ms: number | null
  last_error: string | null
}

export interface ScannerStartResponse {
  started: boolean
  reason?: 'already_running'
}

export interface ScannerStopResponse {
  stopped: boolean
  reason?: 'not_running'
}

export interface PaperStartResponse {
  started: boolean
  reason?: 'not_available' | 'already_running'
}

export interface PaperStopResponse {
  stopped: boolean
  reason?: 'not_running'
}

export type PaperIntentStatus = 'OPEN' | 'FILLED' | 'CANCELED' | 'CANCELLED' | 'EXPIRED' | 'NEW'

export interface PaperIntent {
  id: string
  ticker: string
  universe?: string
  side: 'YES' | 'NO'
  quote_price_cents: number
  size_contracts: number
  created_ts_ms: number
  status: string
  last_seen_best_bid: number | null
  queue_ahead_contracts: number | null
  last_update_ts_ms: number | null
}

export interface PaperFill {
  id: string
  intent_id: string
  universe?: string
  fill_ts_ms: number
  fill_price_cents: number
  fill_size: number
  remaining_size: number
  queue_ahead_at_fill: number | null
  fill_fee_cents: number | null
  ticker: string
  side: 'YES' | 'NO'
}

export interface PaperPosition {
  ticker: string
  universe?: string
  side: 'YES' | 'NO'
  net_contracts: number
  avg_entry_price_cents: number
  realized_pnl_cents: number
  first_open_ts_ms: number
  last_update_ts_ms: number
}

export interface PaperPnlSnapshot {
  ts_ms: number | null
  realized_pnl_cents: number
  unrealized_pnl_cents: number | null
  total_pnl_cents: number | null
  unrealized_pnl_status?: string
  open_positions_count: number
  open_orders_count: number
}

export interface PaperSummaryResponse {
  enabled: boolean
  total_intents_created: number
  total_fills: number
  total_positions: number
  realized_pnl_cents: number
  unrealized_pnl_cents: number | null
  total_pnl_cents: number | null
  marked_unrealized_pnl_cents?: number | null
  unmarked_contracts?: number
  unmarked_cost_basis_cents?: number
  latest_snapshot_ts_ms: number | null
  intent_fill_rate?: number
  raw_fills_per_intent_ratio?: number
  intents_with_at_least_one_fill?: number
  fully_filled_intents?: number
  partial_fill_intents?: number
  partial_fill_rate?: number
  average_filled_fraction?: number
  average_time_to_first_fill_seconds?: number | null
  median_time_to_first_fill_seconds?: number | null
  average_time_to_full_fill_seconds?: number | null
  median_time_to_full_fill_seconds?: number | null
  entry_intents_created?: number
  entry_yes_intents_created?: number
  entry_no_intents_created?: number
  entry_fills?: number
  entry_yes_fills?: number
  entry_no_fills?: number
  entry_intents_with_at_least_one_fill?: number
  entry_intent_fill_rate?: number
  entry_fully_filled_intents?: number
  entry_partial_fill_intents?: number
  entry_average_time_to_first_fill_seconds?: number | null
  entry_median_time_to_first_fill_seconds?: number | null
  entry_average_time_to_full_fill_seconds?: number | null
  entry_median_time_to_full_fill_seconds?: number | null
  entry_avg_net_edge_at_entry?: number | null
  entry_avg_capturable_edge_to_cross_at_entry?: number | null
  entry_avg_abs_distance_from_50_at_entry?: number | null
  unique_entry_tickers_count?: number
  unique_entry_ticker_side_pairs_count?: number
  unique_filled_entry_tickers_count?: number
  unique_filled_entry_ticker_side_pairs_count?: number
  entry_intents_created_by_universe?: Record<string, number>
  entry_fills_by_universe?: Record<string, number>
  unrealized_marks_available?: boolean
  unrealized_pnl_status?: string
  unrealized_marked_positions_count?: number
  unrealized_unmarked_positions_count?: number
}

export interface PaperIntentsResponse {
  status: string
  intents: PaperIntent[]
  count: number
}

export interface PaperFillsResponse {
  fills: PaperFill[]
  count: number
}

export interface PaperPositionsResponse {
  positions: PaperPosition[]
  count: number
  total_realized_pnl_cents: number
}

export interface PaperConfigResponse {
  configured: boolean
  enabled: boolean
  active: boolean
  min_edge_cents: number
  fill_horizon_seconds: number
  poll_interval_seconds: number
  fill_probability_price_moved: number
  fill_probability_at_best: number
  fill_probability_price_improves: number
  simulated_volume_per_poll: number
  max_hold_seconds: number
  take_profit_cents: number
  ticker_cooldown_seconds: number
  max_intents_per_minute: number
  max_concurrent_intents: number
  max_open_orders: number
  max_pos_per_ticker: number
}

export interface PaperIntentDebugResponse {
  intent: Record<string, unknown>
  top_of_book: Record<string, unknown>
  simulator: {
    best_yes_bid: number | null
    best_no_bid: number | null
    cross_yes: number | null
    cross_no: number | null
    best_bid: number | null
    best_size_at_bid: number | null
    queue_ahead_before: number
    queue_ahead_after: number
    eligibility: Record<string, unknown>
    chosen_side: string | null
    price_improves: boolean
    traded_volume_estimate: number
    market_best_bid: number | null
    cross_for_side: number | null
    effective_best_bid: number | null
  }
}

export interface PaperMetricsResponse {
  fill_rate: number
  avg_time_to_fill_seconds: number | null
  fills_last_1h: number
  intents_last_1h: number
}

export type TopSignalsSnapshot = TopSignalsResponse

export interface SignalResponse {
  ticker: string
  universe?: string
  reason_code: string
  net_edge_yes: number | null
  net_edge_no: number | null
  inside_width_yes: number | null
  inside_width_no: number | null
  bid_yes: number | null
  cross_yes: number | null
  bid_no: number | null
  cross_no: number | null
  bid_sum: number | null
  imbalance_gap: number | null
  liq_yes: number | null
  liq_no: number | null
  top_liquidity: number
  timestamp_ms: number
}

export type OrderbookLevel = [number, number]

export interface Orderbook {
  market_ticker?: string
  yes?: OrderbookLevel[]
  no?: OrderbookLevel[]
  [key: string]: unknown
}

export interface DebugOrderbookResponse {
  ticker: string
  orderbook: Orderbook
  latency_ms: number
  success: boolean
}

interface RequestOptions {
  timeoutMs?: number
  signal?: AbortSignal
}

function mergeAbortSignals(
  externalSignal: AbortSignal | undefined,
  timeoutSignal: AbortSignal
): AbortSignal {
  if (!externalSignal) {
    return timeoutSignal
  }

  const mergedController = new AbortController()
  const abortMerged = () => mergedController.abort()

  if (externalSignal.aborted || timeoutSignal.aborted) {
    abortMerged()
    return mergedController.signal
  }

  externalSignal.addEventListener('abort', abortMerged, { once: true })
  timeoutSignal.addEventListener('abort', abortMerged, { once: true })

  return mergedController.signal
}

async function fetchWithErrorHandling<T>(
  url: string,
  { timeoutMs = DEFAULT_TIMEOUT_MS, signal }: RequestOptions = {},
  init?: RequestInit
): Promise<T> {
  const timeoutController = new AbortController()
  const timeoutHandle = setTimeout(() => timeoutController.abort(), timeoutMs)
  const requestSignal = mergeAbortSignals(signal, timeoutController.signal)

  try {
    const response = await fetch(url, {
      ...init,
      signal: requestSignal,
    })
    if (!response.ok) {
      const responseText = (await response.text()).trim()
      const suffix = responseText ? ` - ${responseText}` : ''
      throw new Error(`Request failed (${response.status})${suffix}`)
    }
    return (await response.json()) as T
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error('Request aborted')
    }
    if (err instanceof Error) {
      throw err
    }
    throw new Error('Unknown request error')
  } finally {
    clearTimeout(timeoutHandle)
  }
}

export const api = {
  async health(options?: RequestOptions): Promise<HealthResponse> {
    return fetchWithErrorHandling(`${API_BASE_URL}/health`, options)
  },

  async scanSignals(
    tickers: string[],
    options?: RequestOptions
  ): Promise<SignalResponse[]> {
    const tickerParam = tickers.slice(0, 20).join(',')
    return fetchWithErrorHandling(
      `${API_BASE_URL}/signals/scan?tickers=${encodeURIComponent(tickerParam)}`,
      options
    )
  },

  async liveSignal(ticker: string, options?: RequestOptions): Promise<SignalResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/signals/live?ticker=${encodeURIComponent(ticker)}`,
      options
    )
  },

  async debugOrderbook(
    ticker: string,
    options?: RequestOptions
  ): Promise<DebugOrderbookResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/debug/orderbook/${encodeURIComponent(ticker)}`,
      options
    )
  },

  async topSignals(options?: RequestOptions): Promise<TopSignalsResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/signals/top`,
      options
    )
  },

  async startScanner(options?: RequestOptions): Promise<ScannerStartResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/scanner/start`,
      options,
      { method: 'POST' }
    )
  },

  async stopScanner(options?: RequestOptions): Promise<ScannerStopResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/scanner/stop`,
      options,
      { method: 'POST' }
    )
  },

  async getScannerStatus(options?: RequestOptions): Promise<ScannerStatusResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/scanner/status`,
      options
    )
  },

  async startPaperTrading(options?: RequestOptions): Promise<PaperStartResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/start`,
      options,
      { method: 'POST' }
    )
  },

  async stopPaperTrading(options?: RequestOptions): Promise<PaperStopResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/stop`,
      options,
      { method: 'POST' }
    )
  },

  async getPaperSummary(options?: RequestOptions): Promise<PaperSummaryResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/summary`,
      options
    )
  },

  async getPaperPnlSnapshot(options?: RequestOptions): Promise<PaperPnlSnapshot> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/pnl-snapshot`,
      options
    )
  },

  async getPaperIntents(
    status: PaperIntentStatus = 'OPEN',
    limit = 50,
    options?: RequestOptions
  ): Promise<PaperIntentsResponse> {
    const normalizedStatus = status === 'CANCELLED' ? 'CANCELED' : status
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/intents?status=${encodeURIComponent(normalizedStatus)}&limit=${limit}`,
      options
    )
  },

  async getPaperFills(limit = 50, options?: RequestOptions): Promise<PaperFillsResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/fills?limit=${limit}`,
      options
    )
  },

  async getPaperPositions(options?: RequestOptions): Promise<PaperPositionsResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/positions`,
      options
    )
  },

  async getPaperConfig(options?: RequestOptions): Promise<PaperConfigResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/config`,
      options
    )
  },

  async getPaperIntentDebug(
    intentId: string,
    options?: RequestOptions
  ): Promise<PaperIntentDebugResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/debug/intent/${encodeURIComponent(intentId)}`,
      options
    )
  },

  async getPaperMetrics(options?: RequestOptions): Promise<PaperMetricsResponse> {
    return fetchWithErrorHandling(
      `${API_BASE_URL}/paper/metrics`,
      options
    )
  },
}
