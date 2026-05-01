/**
 * Bot creation validation logic.
 *
 * Enforces minimum order size constraints based on:
 * - Nado as maker: total >= 1000 USD, per-chunk >= 100 USD
 * - All exchanges: per-chunk >= min_order_size (base qty)
 */

import type { SymbolMeta } from '@/types/bot'

// Nado-specific constants
export const NADO_MAKER_MIN_TOTAL_USD = 1000
export const NADO_MAKER_MIN_CHUNK_NOTIONAL_USD = 100

export type ValidationResult =
  | { ok: true; warnings?: string[] }
  | { ok: false; code: string; message: string }

export interface ValidationInput {
  makerExchange: string
  longExchange: string
  shortExchange: string
  longSymbol: string
  shortSymbol: string
  quantity: number // base qty
  numChunks: number
  livePrice: number // USD per base token
  makerMeta: SymbolMeta | null
  takerMeta: SymbolMeta | null
}

/**
 * Validate bot creation parameters against exchange-specific minimums.
 *
 * Rules:
 * 1. If maker is Nado:
 *    - total_notional >= 1000 USD
 *    - chunk_notional >= 100 USD
 * 2. For maker exchange with known min_order_size:
 *    - chunk_qty >= min_order_size
 * 3. For taker exchange with known min_order_size:
 *    - chunk_qty >= min_order_size (soft warning only)
 */
export function validateBotCreate(input: ValidationInput): ValidationResult {
  const {
    makerExchange,
    quantity,
    numChunks,
    livePrice,
    makerMeta,
    takerMeta,
  } = input

  if (quantity <= 0 || !Number.isFinite(quantity)) {
    return { ok: false, code: 'invalid_quantity', message: 'Quantity must be greater than 0' }
  }

  if (numChunks < 1 || !Number.isInteger(numChunks)) {
    return { ok: false, code: 'invalid_chunks', message: 'Chunk count must be a positive integer' }
  }

  const chunkQty = quantity / numChunks
  const totalNotional = quantity * livePrice
  const chunkNotional = totalNotional / numChunks

  // Rule 1: Nado as maker special constraints
  if (makerExchange === 'nado') {
    if (totalNotional < NADO_MAKER_MIN_TOTAL_USD) {
      return {
        ok: false,
        code: 'nado_total_below_min',
        message: `Nado as Maker requires minimum ${NADO_MAKER_MIN_TOTAL_USD} USD total position (current: ${totalNotional.toFixed(2)} USD)`,
      }
    }

    if (chunkNotional < NADO_MAKER_MIN_CHUNK_NOTIONAL_USD) {
      const minChunks = Math.ceil(totalNotional / NADO_MAKER_MIN_CHUNK_NOTIONAL_USD)
      return {
        ok: false,
        code: 'nado_chunk_below_min',
        message: `Nado as Maker requires minimum ${NADO_MAKER_MIN_CHUNK_NOTIONAL_USD} USD per chunk. Reduce to ${minChunks} chunks or increase position size.`,
      }
    }
  }

  // Rule 2: Maker min_order_size (base qty)
  if (makerMeta && makerMeta.min_order_size > 0) {
    if (chunkQty < makerMeta.min_order_size) {
      const minQty = makerMeta.min_order_size * numChunks
      return {
        ok: false,
        code: 'maker_chunk_below_min_qty',
        message: `${makerExchange} Maker requires minimum ${makerMeta.min_order_size} ${makerMeta.base_token} per chunk. Increase quantity to at least ${minQty.toFixed(6)} or reduce chunks.`,
      }
    }
  }

  // Rule 3: Taker min_order_size (soft check - warning only)
  const warnings: string[] = []
  if (takerMeta && takerMeta.min_order_size > 0) {
    if (chunkQty < takerMeta.min_order_size) {
      warnings.push(
        `Warning: ${takerMeta.exchange} Taker minimum order size is ${takerMeta.min_order_size} ${takerMeta.base_token}, but each chunk is ${chunkQty.toFixed(6)}. ` +
        `The engine may auto-reduce chunks at runtime.`,
      )
    }
  }

  return { ok: true, warnings: warnings.length > 0 ? warnings : undefined }
}

/**
 * Compute the effective minimum quantity for display purposes.
 *
 * Currently simplified: only base-qty floor, not notional conversion
 * (matches OMSv2 behavior where computeEffectiveMinQty ignores minNotionalUsd).
 * The `_midPrice` parameter is reserved for the future notional-conversion path.
 */
export function computeEffectiveMinQty(
  meta: SymbolMeta | null,
  _midPrice: number,
): number {
  if (!meta) return 0
  return meta.min_order_size > 0 ? meta.min_order_size : 0
}

/**
 * Format a validation result for display in the UI.
 */
export function formatValidationMessage(result: ValidationResult): string {
  if (result.ok) {
    return result.warnings?.join('\n') || ''
  }
  return result.message
}
