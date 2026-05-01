/**
 * OMS V2 tradeability client.
 *
 * Mirrors deploy/cf-containers/oms-v2/src/lib/tradeability.ts.
 *
 * Calls /tracked/legs which is the standard /tracked map annotated with the
 * per-exchange `tradeable` flag the OMS V2 evaluates on each cron tick.
 * The frontend uses it to hide tokens whose required exchanges are not
 * actually tradeable (e.g. Nado/ARB-PERP — listed but only one-sided book).
 *
 * Backend default: when no tradeability check has run yet (worker cold
 * start), `tradeable` is `true` everywhere so we don't falsely hide
 * legitimate symbols.
 */

const OMS_BASE = import.meta.env.VITE_OMS_META_URL

export type TradeabilityReason =
  | 'no_book'
  | 'no_bids'
  | 'no_asks'
  | 'disconnected'
  | 'stale'
  | 'crossed_book'
  | 'spread_too_wide'
  | 'invalid_price'

export interface TradeabilityLeg {
  symbol: string
  tradeable: boolean
  reason: TradeabilityReason | null
  checked_at: number | null
}

export interface AnnotatedTrackedResponse {
  /** V1-shape mirror: `{ TOKEN: { exchange: symbol, ... } }`. */
  pairs: Record<string, Record<string, string>>
  /** Annotated mirror: `{ TOKEN: { exchange: { symbol, tradeable, reason } } }`. */
  legs: Record<string, Record<string, TradeabilityLeg>>
}

interface CacheEntry {
  data: AnnotatedTrackedResponse
  timestamp: number
}

let cache: CacheEntry | null = null
const CACHE_TTL_MS = 60 * 1000 // 1 min — backend updates every 15 min anyway

/**
 * Fetch the annotated /tracked/legs map. Cached for 60 s.
 *
 * Returns `null` on network / configuration failure; the caller should
 * treat that as "tradeability data unavailable" and fall back to showing
 * all tokens (fail-open). Hiding everything on a transient OMS hiccup
 * would block the user from creating bots entirely.
 */
export async function fetchTrackedWithTradeability(): Promise<AnnotatedTrackedResponse | null> {
  if (cache && Date.now() - cache.timestamp < CACHE_TTL_MS) {
    return cache.data
  }

  if (!OMS_BASE) {
    console.warn('[oms-tradeability] VITE_OMS_META_URL not set, skipping tradeability fetch')
    return null
  }

  try {
    const resp = await fetch(`${OMS_BASE}/tracked/legs`, { method: 'GET' })
    if (!resp.ok) {
      console.warn(`[oms-tradeability] HTTP ${resp.status} from /tracked/legs`)
      return null
    }
    const data = (await resp.json()) as AnnotatedTrackedResponse
    if (!data || typeof data !== 'object' || !data.legs) {
      console.warn('[oms-tradeability] malformed /tracked/legs payload')
      return null
    }
    cache = { data, timestamp: Date.now() }
    return data
  } catch (err) {
    console.warn('[oms-tradeability] fetch failed:', err)
    return null
  }
}

/**
 * Return true if `token` is tradeable on every exchange in `exchanges`.
 * Fail-open: if `legs` does not include the token at all, we assume it is
 * tradeable (the OMS hasn't seen it yet — better than blocking the user).
 */
export function isTokenTradeableOn(
  legs: AnnotatedTrackedResponse['legs'] | undefined,
  token: string,
  exchanges: readonly string[],
): boolean {
  if (!legs) return true
  const entry = legs[token]
  if (!entry) return true
  for (const exch of exchanges) {
    const leg = entry[exch]
    // Missing leg → not advertised by OMS → cannot trade.
    if (!leg) return false
    if (!leg.tradeable) return false
  }
  return true
}

export function clearTradeabilityCache(): void {
  cache = null
}
