import { useState, useEffect } from 'react'
import { api } from '../lib/api'

interface StatusBannerProps {
  isConnected: boolean
  lastHeartbeat: number | null
}

export function StatusBanner({ isConnected, lastHeartbeat }: StatusBannerProps) {
  return (
    <div className="flex items-center justify-between border-b border-white/10 px-6 py-3 bg-zinc-900/60">
      <div className="flex items-center gap-3">
        <div
          className={`w-2 h-2 rounded-full ${
            isConnected ? 'bg-emerald-500' : 'bg-red-500'
          }`}
        />
        <span className="text-xs text-zinc-400">
          {isConnected ? 'Connected' : 'Disconnected'}
        </span>
      </div>
      {lastHeartbeat && (
        <span className="text-xs text-zinc-500 tabular-nums">
          {new Date(lastHeartbeat).toLocaleTimeString()}
        </span>
      )}
    </div>
  )
}

export function APIErrorBanner({ show, message }: { show: boolean; message: string }) {
  if (!show) return null
  return (
    <div className="border-b border-red-500/30 bg-red-950/30 px-6 py-3">
      <p className="text-sm text-red-300">
        API Error: {message}
      </p>
    </div>
  )
}

export function useHealthCheck(interval: number = 5000) {
  const [isConnected, setIsConnected] = useState(false)
  const [lastHeartbeat, setLastHeartbeat] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const checkHealth = async () => {
      try {
        await api.health()
        setIsConnected(true)
        setLastHeartbeat(Date.now())
        setError(null)
      } catch (err) {
        setIsConnected(false)
        setError(err instanceof Error ? err.message : 'Connection failed')
      }
    }

    checkHealth()
    const timer = setInterval(checkHealth, interval)
    return () => clearInterval(timer)
  }, [interval])

  return { isConnected, lastHeartbeat, error }
}
