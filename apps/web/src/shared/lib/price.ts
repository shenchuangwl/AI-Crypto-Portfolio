/** Binance USDT-M price display: never default to 2dp (turns 0.01587 into 0.02). */

export function clampPricePrecision(n: number): number {
  if (!Number.isFinite(n)) return 2;
  return Math.min(8, Math.max(0, Math.round(n)));
}

/** Digits from a live price when exchange precision is missing. */
export function digitsFromPrice(v: number): number {
  const x = Math.abs(v);
  if (!Number.isFinite(x) || x === 0) return 2;
  if (x >= 1000) return 2;
  if (x >= 100) return 2;
  if (x >= 1) return 4;
  if (x >= 0.1) return 5;
  if (x >= 0.01) return 6;
  if (x >= 0.001) return 7;
  return 8;
}

/** Count trailing significant decimals of a finite number (cap 8). */
export function decimalsOf(v: number): number {
  if (!Number.isFinite(v)) return 0;
  const s = v.toFixed(8).replace(/\.?0+$/, '');
  const i = s.indexOf('.');
  return i < 0 ? 0 : s.length - i - 1;
}

export function resolvePricePrecision(opts: {
  declared?: number | null;
  samples?: Array<number | null | undefined>;
}): number {
  const declared =
    opts.declared != null && Number.isFinite(opts.declared) ? clampPricePrecision(opts.declared) : null;
  const samples = (opts.samples || []).filter((x): x is number => x != null && Number.isFinite(x) && x !== 0);
  if (samples.length) {
    const last = samples[samples.length - 1];
    const inferred = clampPricePrecision(
      Math.max(digitsFromPrice(last), ...samples.slice(-40).map(decimalsOf)),
    );
    // Live OHLC/last wins. Declared exchange digits are only a floor when
    // samples are missing — padding BMT to 7dp (0.0158500) is unnecessary,
    // but dropping to 2dp would reprint 0.01587 as 0.02.
    return inferred;
  }
  return declared ?? 2;
}

export function minMove(precision: number): number {
  const p = clampPricePrecision(precision);
  return Number((10 ** -p).toFixed(p));
}

export function formatPrice(v: number | null | undefined, precision?: number | null): string {
  if (v == null || Number.isNaN(v)) return '—';
  const d = resolvePricePrecision({ declared: precision, samples: [v] });
  return v.toFixed(d);
}
