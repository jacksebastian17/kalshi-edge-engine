import { useState } from 'react'

interface WatchlistInputProps {
  onScan: (tickers: string[]) => void
  isLoading: boolean
  pollInterval: number
  onPollIntervalChange: (interval: number) => void
  isPaused: boolean
  onPausedChange: (paused: boolean) => void
}

export function WatchlistInput({
  onScan,
  isLoading,
  pollInterval,
  onPollIntervalChange,
  isPaused,
  onPausedChange,
}: WatchlistInputProps) {
  const [watchlist, setWatchlist] = useState('')

  const handleScan = () => {
    const tickers = watchlist
      .split(',')
      .map((t) => t.trim().toUpperCase())
      .filter((t) => t.length > 0)

    if (tickers.length > 0) {
      onScan(tickers)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      handleScan()
    }
  }

  return (
    <div className="border-b border-white/10 bg-zinc-900/60 p-6 space-y-4">
      <div>
        <label className="block text-xs text-zinc-400 mb-2">
          Watchlist (comma-separated)
        </label>
        <textarea
          value={watchlist}
          onChange={(e) => setWatchlist(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="WEATHER_IV_FALL, SENATE_BILL_123, METR_TEMP_JAN"
          className="w-full bg-zinc-900 border border-white/10 rounded px-4 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-white/20 focus:ring-1 focus:ring-white/20"
          rows={2}
        />
        <p className="mt-1 text-xs text-zinc-500">
          Max 20 tickers. Ctrl+Enter to scan.
        </p>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <button
          onClick={handleScan}
          disabled={isLoading}
          className="bg-emerald-700 hover:bg-emerald-600 disabled:bg-zinc-700 text-white px-4 py-2 rounded text-sm transition-colors"
        >
          {isLoading ? 'Scanning...' : 'Scan'}
        </button>

        <div>
          <label className="block text-xs text-zinc-400 mb-2">
            Poll Interval (ms)
          </label>
          <select
            value={pollInterval}
            onChange={(e) => onPollIntervalChange(Number(e.target.value))}
            className="w-full bg-zinc-900 border border-white/10 rounded px-2 py-2 text-xs text-zinc-100 focus:outline-none focus:border-white/20"
          >
            <option value={1000}>1s</option>
            <option value={2000}>2s</option>
            <option value={5000}>5s</option>
          </select>
        </div>

        <button
          onClick={() => onPausedChange(!isPaused)}
          className={`px-4 py-2 rounded text-sm font-mono transition-colors ${
            isPaused
              ? 'bg-amber-700 hover:bg-amber-600 text-white'
              : 'bg-zinc-800 hover:bg-zinc-700 text-zinc-100 border border-white/10'
          }`}
        >
          {isPaused ? 'Paused' : 'Polling'}
        </button>
      </div>
    </div>
  )
}
