/**
 * OMS V2 /meta endpoint client.
 *
 * Fetches symbol metadata (tick_size, min_order_size, min_notional_usd, etc.)
 * directly from the Cloudflare Worker for use in frontend validation.
 */

import type { SymbolMeta } from '@/types/bot'

const OMS_META_BASE = import.meta.env.VITE_OMS_META_URL

interface CacheEntry {
  meta: SymbolMeta
  timestamp: number
}

const cache = new Map<string, CacheEntry>()
const CACHE_TTL_MS = 5 * 60 * 1000 // 5 minutes

function cacheKey(exchange: string, symbol: string): string {
  return `${exchange}:${symbol}`
}

/**
 * Fetch symbol metadata from OMS V2 /meta endpoint.
 * Results are cached for 5 minutes.
 */
export async function fetchSymbolMeta(
  exchange: string,
  symbol: string,
): Promise<SymbolMeta | null> {
  const key = cacheKey(exchange, symbol)
  const cached = cache.get(key)
  if (cached && Date.now() - cached.timestamp < CACHE_TTL_MS) {
    return cached.meta
  }

  if (!OMS_META_BASE) {
    console.warn('[oms-meta] VITE_OMS_META_URL not set, skipping meta fetch')
    return null
  }

  const url = `${OMS_META_BASE}/meta/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}`
  try {
    const resp = await fetch(url, { method: 'GET' })
    if (!resp.ok) {
      if (resp.status === 404) {
        console.warn(`[oms-meta] No meta found for ${exchange}:${symbol}`)
      } else {
        console.error(`[oms-meta] Failed to fetch meta for ${exchange}:${symbol}: ${resp.status}`)
      }
      return null
    }
    const meta: SymbolMeta = await resp.json()
    cache.set(key, { meta, timestamp: Date.now() })
    return meta
  } catch (err) {
    console.error(`[oms-meta] Error fetching meta for ${exchange}:${symbol}:`, err)
    return null
  }
}

/**
 * Get cached meta without fetching. Returns null if not cached or expired.
 */
export function getCachedMeta(exchange: string, symbol: string): SymbolMeta | null {
  const key = cacheKey(exchange, symbol)
  const cached = cache.get(key)
  if (cached && Date.now() - cached.timestamp < CACHE_TTL_MS) {
    return cached.meta
  }
  return null
}

/**
 * Preload meta for a set of (exchange, symbol) pairs.
 * Useful when stepping through the wizard.
 */
export async function preloadMeta(pairs: Array<{ exchange: string; symbol: string }>): Promise<void> {
  await Promise.all(
    pairs.map(({ exchange, symbol }) => fetchSymbolMeta(exchange, symbol).catch(() => null)),
  )
}

/**
 * Clear the cache. Useful for testing or when discovery is known to have run.
 */
export function clearMetaCache(): void {
  cache.clear()
}
