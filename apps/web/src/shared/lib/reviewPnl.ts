/** Realized PnL vs exit price. Storage is a decimal fraction (0.0231 = +2.31%). */

export type RealizedPnl = { pct: number; sign: 1 | 0 | -1 };

const FLAT_EPS = 1e-12;

function asPrice(v: number | null | undefined): number | null {
  if (v == null) return null;
  const x = Number(v);
  if (!Number.isFinite(x) || x === 0) return null;
  return x;
}

export function realizedPnl(
  direction: 'up' | 'down',
  stay: number | null | undefined,
  exitp: number | null | undefined,
): RealizedPnl | null {
  const s = asPrice(stay);
  const e = asPrice(exitp);
  if (s == null || e == null) return null;
  const pct = direction === 'up' ? e / s - 1 : 1 - e / s;
  if (!Number.isFinite(pct)) return null;
  if (Math.abs(pct) < FLAT_EPS) return { pct: 0, sign: 0 };
  return { pct, sign: pct > 0 ? 1 : -1 };
}
