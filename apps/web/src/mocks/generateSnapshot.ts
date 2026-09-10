import type {
  CandidateRow,
  Direction,
  LiquidityGrade,
  McapTfDetail,
  McapTfGrade,
  McapTimeframe,
  ScreenerSnapshot,
  ScreenerState,
} from '../shared/types/screener';
import { MCAP_TIMEFRAMES } from '../shared/types/screener';

const UNDERLYINGS = [
  'BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'ADA', 'AVAX', 'LINK', 'DOT', 'NEAR',
  'APT', 'ARB', 'OP', 'SUI', 'TIA', 'SEI', 'INJ', 'FET', 'RENDER', 'WIF',
  'PEPE', 'BONK', 'ORDI', 'STX', 'FIL', 'ATOM', 'LTC', 'BCH', 'UNI', 'AAVE',
  'MKR', 'CRV', 'LDO', 'ENA', 'W', 'JUP', 'PYTH', 'JTO', 'STRK', 'MANTA',
  'ALT', 'PIXEL', 'PORTAL', 'AEVO', 'ETHFI', 'BOME', 'WLD', 'TAO', 'ONDO', 'ZRO',
];

function mulberry32(seed: number) {
  return () => {
    let t = (seed += 0x6d2b79f5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pickState(r: number, score: number): ScreenerState {
  if (r < 0.04) return 'DATA_INSUFFICIENT';
  if (r < 0.07) return 'LOW_CONFIDENCE';
  if (r < 0.18) return 'ELIMINATED';
  if (score >= 78) return 'CONFIRMED';
  if (score >= 62) return 'QUALIFIED';
  if (score >= 48) return 'WATCH';
  return 'ELIMINATED';
}

function gradeFromLiquidity(s: number): LiquidityGrade {
  if (s >= 80) return 'A';
  if (s >= 65) return 'B';
  if (s >= 50) return 'C';
  return 'D';
}

/**
 * MOCK ONLY —— 后端 `coin_selection.mcap_timeframe.grade_from_mas` 的镜像。
 * 生产板面的等级一律由后端下发；前端绝不重算真实数据的等级。
 *
 *   A：MA6 > MA12 > MA26 | B：MA12 > MA6 > MA26 | C：MA12 > MA26 > MA6
 *   D：MA12 < MA26 < MA6 | E：MA12 < MA6 < MA26 | F：MA6 < MA12 < MA26
 */
function mockGradeFromMas(ma6: number, ma12: number, ma26: number): McapTfGrade | null {
  if (ma6 > ma12 && ma12 > ma26) return 'A';
  if (ma12 > ma6 && ma6 > ma26) return 'B';
  if (ma12 > ma26 && ma26 > ma6) return 'C';
  if (ma12 < ma26 && ma26 < ma6) return 'D';
  if (ma12 < ma6 && ma6 < ma26) return 'E';
  if (ma6 < ma12 && ma12 < ma26) return 'F';
  return null;
}

/** 造 26 根带漂移的收盘价 → 流通市值序列 → 6/12/26 根均线 → 判级。 */
function mockMcapTf(
  rand: () => number,
  supply: number | null,
  multiplier: number,
): Partial<Record<McapTimeframe, McapTfDetail>> {
  const out: Partial<Record<McapTimeframe, McapTfDetail>> = {};
  for (const tf of MCAP_TIMEFRAMES) {
    const bars = rand() < 0.04 ? Math.floor(rand() * 25) : 26;
    if (bars < 26) {
      out[tf] = {
        grade: null,
        ma6: null,
        ma12: null,
        ma26: null,
        bars,
        supply_known: supply != null,
        reason: 'insufficient_bars<26',
      };
      continue;
    }
    const drift = (rand() - 0.5) * 0.02;
    const factor = (supply ?? 1) / multiplier;
    const series: number[] = [];
    let px = 1 + rand();
    for (let i = 0; i < 26; i += 1) {
      px *= 1 + drift + (rand() - 0.5) * 0.01;
      series.push(px * factor);
    }
    const mean = (k: number) => series.slice(-k).reduce((a, b) => a + b, 0) / k;
    const ma6 = mean(6);
    const ma12 = mean(12);
    const ma26 = mean(26);
    const grade = mockGradeFromMas(ma6, ma12, ma26);
    out[tf] = {
      grade,
      ma6: supply == null ? null : ma6,
      ma12: supply == null ? null : ma12,
      ma26: supply == null ? null : ma26,
      bars: 26,
      supply_known: supply != null,
      reason: grade == null ? 'flat_or_tie' : supply == null ? 'ok_supply_unknown' : 'ok',
    };
  }
  return out;
}

function makeRow(
  i: number,
  direction: Direction,
  rand: () => number,
  anchorIso: string,
): CandidateRow {
  const base = UNDERLYINGS[i % UNDERLYINGS.length];
  const suffix = i >= UNDERLYINGS.length ? String(Math.floor(i / UNDERLYINGS.length)) : '';
  const underlying = `${base}${suffix}`;
  const score_up = +(40 + rand() * 55).toFixed(1);
  const score_down = +(10 + rand() * 50).toFixed(1);
  const activeScore = direction === 'up' ? score_up : score_down;
  const state = pickState(rand(), activeScore);
  const staircase = +(30 + rand() * 60).toFixed(0);
  const liq = +(45 + rand() * 50).toFixed(0);
  const enterMin = Math.floor(rand() * 360);
  const multiplier = underlying.startsWith('1000') ? 1000 : 1;
  const supply = state === 'DATA_INSUFFICIENT' ? null : Math.floor(1e8 + rand() * 1e11);
  const mcapTf = mockMcapTf(rand, supply, multiplier);
  const enter = new Date(Date.parse(anchorIso) + enterMin * 60_000);

  return {
    rank: 0,
    symbol: `${underlying}USDT`,
    underlying_asset: underlying,
    canonical_asset_id: underlying.toLowerCase(),
    contract_multiplier: multiplier,
    direction,
    state,
    state_enter_time_utc: enter.toISOString(),
    state_duration_minutes: Math.max(15, 360 - enterMin),
    state_enter_price:
      state === 'WATCH' || state === 'QUALIFIED' || state === 'CONFIRMED'
        ? +(0.01 + rand() * 100).toFixed(4)
        : null,
    score_up,
    score_down,
    direction_confidence: +Math.min(1, 0.4 + rand() * 0.6).toFixed(2),
    liquidity_score: liq,
    liquidity_grade: gradeFromLiquidity(liq),
    mcap_grade_30m: mcapTf['30m']?.grade ?? null,
    mcap_grade_2h: mcapTf['2h']?.grade ?? null,
    mcap_grade_6h: mcapTf['6h']?.grade ?? null,
    mcap_tf: mcapTf,
    momentum_score: +(40 + rand() * 50).toFixed(0),
    mcap_momentum_score: +(40 + rand() * 45).toFixed(0),
    staircase_score: staircase,
    consistency_score: +(0.5 + rand() * 0.5).toFixed(2),
    rank_velocity_score: +(40 + rand() * 50).toFixed(0),
    risk_score: state === 'ELIMINATED' ? +(40 + rand() * 30).toFixed(0) : +(85 + rand() * 15).toFixed(0),
    data_confidence: state === 'LOW_CONFIDENCE' ? +(40 + rand() * 20).toFixed(0) : +(80 + rand() * 20).toFixed(0),
    ret_15m: +((rand() - 0.45) * 0.03).toFixed(4),
    ret_1h: +((rand() - 0.4) * 0.06).toFixed(4),
    ret_4h: +((rand() - 0.35) * 0.1).toFixed(4),
    ret_24h: +((rand() - 0.3) * 0.18).toFixed(4),
    // 周期越长振幅越大；偶尔给 null，好在离线模式下也能看到 `—` 的渲染
    ret_1w: rand() > 0.04 ? +((rand() - 0.3) * 0.35).toFixed(4) : null,
    ret_1mo: rand() > 0.08 ? +((rand() - 0.3) * 0.6).toFixed(4) : null,
    ret_since_anchor: +((rand() - 0.3) * 0.15).toFixed(4),
    aqv_6d_m: +(8 + rand() * 120).toFixed(0),
    aqv_12d_m: +(8 + rand() * 100).toFixed(0),
    aqv_26d_m: +(8 + rand() * 90).toFixed(0),
    circulating_supply: supply,
    market_cap_coingecko: state === 'DATA_INSUFFICIENT' ? null : Math.floor(2e7 + rand() * 5e9),
    market_cap_calculated: state === 'DATA_INSUFFICIENT' ? null : Math.floor(2e7 + rand() * 5e9),
    supply_source: state === 'DATA_INSUFFICIENT' ? 'NONE' : rand() > 0.15 ? 'CG' : 'CACHE',
    data_mode:
      state === 'DATA_INSUFFICIENT'
        ? 'MISSING'
        : rand() > 0.2
          ? 'LIVE'
          : 'CACHE_FRESH',
    supply_as_of_utc: state === 'DATA_INSUFFICIENT' ? null : new Date().toISOString(),
    mapping_confidence: state === 'DATA_INSUFFICIENT' ? 0 : +Math.min(1, 0.7 + rand() * 0.3).toFixed(2),
    risk_flags: state === 'ELIMINATED' ? [rand() > 0.5 ? 'H1' : 'F1'] : rand() > 0.85 ? ['S9'] : [],
    reason_codes:
      activeScore >= 70
        ? ['SS3', 'OI_UP', 'VOL_EXP']
        : activeScore >= 55
          ? ['VOL_EXP']
          : [],
    // Dual-path audit labels mirror pass_qualified / pass_confirmed (param-v1.3.0).
    not_confirmed_reasons:
      state === 'QUALIFIED' ? (staircase >= 50 ? ['dwell'] : ['ss', 'streak']) : [],
    qualified_path:
      state === 'QUALIFIED' || state === 'CONFIRMED'
        ? staircase >= 50
          ? 'S'
          : 'M'
        : null,
    confirmed_path: state === 'CONFIRMED' ? (staircase >= 50 ? 'S' : 'M') : null,
    ready_confirm: state === 'QUALIFIED' && activeScore >= 70,
    ref_price: +(0.01 + rand() * 100).toFixed(4),
  };
}

export function generateSnapshot(opts?: {
  seed?: number;
  poolSize?: number;
  now?: Date;
}): ScreenerSnapshot {
  const seed = opts?.seed ?? 42;
  const poolSize = opts?.poolSize ?? 120;
  const now = opts?.now ?? new Date('2026-08-10T06:16:52Z');
  const rand = mulberry32(seed);

  // Anchor 00:00 UTC of scan day
  const anchor = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 0, 0, 0));
  const scanTs = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 6, 15, 0));
  const seq = Math.floor((scanTs.getTime() - anchor.getTime()) / 900_000);
  const scanId = `${anchor.toISOString().slice(0, 10).replace(/-/g, '')}-${String(seq).padStart(3, '0')}`;

  const long_pool = Array.from({ length: poolSize }, (_, i) =>
    makeRow(i, 'up', rand, anchor.toISOString()),
  )
    .sort((a, b) => b.score_up - a.score_up)
    .map((r, i) => ({ ...r, rank: i + 1 }));

  const short_pool = Array.from({ length: Math.floor(poolSize * 0.8) }, (_, i) =>
    makeRow(i + 200, 'down', rand, anchor.toISOString()),
  )
    .sort((a, b) => b.score_down - a.score_down)
    .map((r, i) => ({ ...r, rank: i + 1 }));

  const all = [...long_pool, ...short_pool];
  const count = (s: ScreenerState) => all.filter((r) => r.state === s).length;
  // Occupancy dedupes by symbol; state_counts counts (symbol, direction) sides.
  const uniq = (s: ScreenerState) =>
    new Set(all.filter((r) => r.state === s).map((r) => r.symbol)).size;
  const confirmedUnique = uniq('CONFIRMED');
  const qualifiedUnique = uniq('QUALIFIED');

  const expires = new Date(scanTs.getTime() + 15 * 60_000);

  return {
    meta: {
      system_version: 'v1.2',
      anchor_date: anchor.toISOString().slice(0, 10),
      scan_id: scanId,
      scan_sequence: seq,
      scan_timestamp_utc: scanTs.toISOString(),
      effective_universe: 528,
      baseline_universe: 526,
      regime: 0.58,
      regime_label: '中性偏多',
      data_mode: 'LIVE',
      state_counts: {
        WATCH: count('WATCH'),
        QUALIFIED: count('QUALIFIED'),
        CONFIRMED: count('CONFIRMED'),
        ELIMINATED: count('ELIMINATED'),
        DATA_INSUFFICIENT: count('DATA_INSUFFICIENT'),
        LOW_CONFIDENCE: count('LOW_CONFIDENCE'),
      },
      coingecko_credits: {
        used_today: 72,
        month_est: 8904,
        month_cap: 10000,
        utilization: 0.89,
        alert_level: 'warn',
      },
      parameter_version: 'param-v1.3.0-dual-path-sticky',
      mapping_version: 'map-20260810',
      data_version: `data-${scanId}`,
      indicator_version: 'g1g2g3g4-sm-dual-path',
      occupancy: {
        sides: {
          WATCH: count('WATCH'),
          QUALIFIED: count('QUALIFIED'),
          CONFIRMED: count('CONFIRMED'),
          ELIMINATED: count('ELIMINATED'),
        },
        confirmed_unique: confirmedUnique,
        qualified_unique: qualifiedUnique,
        ready_confirm: [],
      },
      // Day roll-up ≥ occupancy by construction (zones rotate through the day).
      daily_unique: {
        anchor_date: anchor.toISOString().slice(0, 10),
        confirmed: confirmedUnique + 6,
        dmr: Math.min(confirmedUnique, 16),
        qualified: qualifiedUnique + 18,
        watch: count('WATCH') + 40,
        eliminated: count('ELIMINATED'),
      },
      control: {
        n_impulse: 24,
        live_ratio: 1,
        universe_dev: 0,
        freeze_new_confirm: false,
        low_breadth: false,
        data_stress: false,
        universe_stress: false,
      },
      dmr: {
        unique_before_k: confirmedUnique,
        inbox_k: 16,
        inbox_count: Math.min(confirmedUnique, 16),
        truncated: Math.max(0, confirmedUnique - 16),
        underfilled: confirmedUnique < 10,
      },
      alerts:
        confirmedUnique < 10
          ? ['CONFIRM_UNDERFILLED']
          : confirmedUnique > 40
            ? ['CONFIRM_OVERFLOW']
            : [],
    },
    long_pool,
    short_pool,
    transitions: long_pool
      .filter((r) => r.state === 'CONFIRMED')
      .slice(0, 5)
      .map((r, i) => ({
        id: `t${i}`,
        at_utc: scanTs.toISOString(),
        symbol: r.symbol,
        from_state: 'QUALIFIED' as const,
        to_state: 'CONFIRMED' as const,
        direction: 'up' as const,
        reason_codes: r.reason_codes,
      })),
    generated_at_utc: now.toISOString(),
    expires_at_utc: expires.toISOString(),
    attribution: 'Powered by CoinGecko',
  };
}

export function generateMockKlines(
  symbol: string,
  interval: import('../shared/lib/intervals').KlineInterval,
  limit = 200,
): import('../shared/types/screener').OhlcvBar[] {
  const step = {
    '15m': 900,
    '30m': 1800,
    '1h': 3600,
    '2h': 7200,
    '4h': 14400,
    '6h': 21600,
    '1d': 86400,
  }[interval];
  const seed = symbol.split('').reduce((a, c) => a + c.charCodeAt(0), 0);
  const rand = mulberry32(seed + step);
  const end = Math.floor(Date.parse('2026-08-10T06:15:00Z') / 1000);
  let price = 20 + (seed % 80);
  const bars = [];
  for (let i = limit - 1; i >= 0; i--) {
    const time = end - i * step;
    const drift = (rand() - 0.48) * price * 0.02;
    const open = price;
    const close = Math.max(0.01, open + drift);
    const high = Math.max(open, close) * (1 + rand() * 0.008);
    const low = Math.min(open, close) * (1 - rand() * 0.008);
    const volume = 1000 + rand() * 50000;
    bars.push({ time, open, high, low, close, volume });
    price = close;
  }
  return bars;
}
