import { SignalResponse } from '../lib/api'

interface SignalsTableProps {
  signals: SignalResponse[]
  isLoading: boolean
  onRowClick: (signal: SignalResponse) => void
}

/**
 * Color pill for reason_code based on signal validity.
 */
function ReasonCodePill({ code }: { code: string }) {
  const colorMap: Record<string, string> = {
    SIGNAL_VALID: 'bg-emerald-900/70 text-emerald-200 border border-emerald-700/50',
    SPREAD_TOO_SMALL: 'bg-amber-900/70 text-amber-200 border border-amber-700/50',
    LOW_LIQUIDITY: 'bg-orange-900/70 text-orange-200 border border-orange-700/50',
    MARKET_INELIGIBLE: 'bg-red-900/70 text-red-200 border border-red-700/50',
    DATA_STALE: 'bg-violet-900/70 text-violet-200 border border-violet-700/50',
    RISK_LIMIT_HIT: 'bg-rose-900/70 text-rose-200 border border-rose-700/50',
  }

  const classes = colorMap[code] || 'bg-zinc-800 text-zinc-200 border border-white/10'

  return (
    <span
      className={`inline-block px-3 py-1 text-xs font-mono rounded ${classes}`}
    >
      {code}
    </span>
  )
}

/**
 * Format number for monospace display.
 */
function formatEdge(value: number | null): string {
  if (value === null) return '—'
  return value.toFixed(2)
}

function edgeColorClass(value: number | null): string {
  if (value === null) return 'text-zinc-500'
  if (value < 0) return 'text-red-300'
  return 'text-emerald-300'
}

function formatLiquidity(value: number): string {
  return value.toLocaleString()
}

export function SignalsTable({
  signals,
  isLoading,
  onRowClick,
}: SignalsTableProps) {
  return (
    <div className="flex-1 overflow-hidden flex flex-col">
      {isLoading && (
        <div className="border-b border-white/10 px-6 py-2 bg-zinc-900/60">
          <span className="text-xs text-zinc-400 animate-pulse">
            Loading signals...
          </span>
        </div>
      )}

      {signals.length === 0 && !isLoading && (
        <div className="flex-1 flex items-center justify-center text-zinc-500">
          <p className="text-sm">No signals. Enter tickers and click Scan.</p>
        </div>
      )}

      {signals.length > 0 && (
        <div className="flex-1 overflow-auto">
          <table className="w-full text-xs border-collapse">
            <thead className="sticky top-0 bg-zinc-900/80 backdrop-blur-sm">
              <tr className="border-b border-white/10">
                <th className="px-4 py-2 text-left text-zinc-300">
                  Ticker
                </th>
                <th className="px-4 py-2 text-left text-zinc-300">
                  Reason
                </th>
                <th className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                  Net Edge Y
                </th>
                <th className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                  Net Edge N
                </th>
                <th className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                  Width Y
                </th>
                <th className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                  Width N
                </th>
                <th className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                  Liquidity
                </th>
                <th className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                  Gap
                </th>
              </tr>
            </thead>
            <tbody>
              {signals.map((signal) => (
                <tr
                  key={signal.ticker}
                  onClick={() => onRowClick(signal)}
                  className="border-b border-white/10 hover:bg-zinc-900/60 cursor-pointer transition-colors"
                >
                  <td className="px-4 py-2 text-zinc-100 font-bold">
                    {signal.ticker}
                  </td>
                  <td className="px-4 py-2">
                    <ReasonCodePill code={signal.reason_code} />
                  </td>
                  <td
                    className={`px-4 py-2 text-right tabular-nums ${edgeColorClass(
                      signal.net_edge_yes
                    )}`}
                  >
                    {formatEdge(signal.net_edge_yes)}
                  </td>
                  <td
                    className={`px-4 py-2 text-right tabular-nums ${edgeColorClass(
                      signal.net_edge_no
                    )}`}
                  >
                    {formatEdge(signal.net_edge_no)}
                  </td>
                  <td className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                    {formatEdge(signal.inside_width_yes)}
                  </td>
                  <td className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                    {formatEdge(signal.inside_width_no)}
                  </td>
                  <td className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                    {formatLiquidity(signal.top_liquidity)}
                  </td>
                  <td className="px-4 py-2 text-right text-zinc-300 tabular-nums">
                    {formatEdge(signal.imbalance_gap)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
