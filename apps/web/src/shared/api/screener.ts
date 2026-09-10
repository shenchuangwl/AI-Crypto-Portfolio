import { generateMockKlines, generateSnapshot } from '../../mocks/generateSnapshot';
import { boardConfig, DEFAULT_BOARD, type BoardKey } from '../config/boards';
import type { KlineInterval } from '../lib/intervals';
import type {
  CandidateRow,
  Direction,
  OhlcvBar,
  ScanMeta,
  ScreenerSnapshot,
  ScreenerState,
  SymbolScreenerDetail,
  TransitionEvent,
} from '../types/screener';
import { normalizeScreenerState } from '../types/screener';

/**
 * Base URL for api-gateway.
 * - Dev default: Vite proxy `/api` → http://127.0.0.1:18080
 * - Override: VITE_API_BASE=http://127.0.0.1:18080/api/v1
 * - Offline mock: VITE_USE_MOCK=1
 */
const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') || '/api/v1';
const USE_MOCK =
  import.meta.env.VITE_USE_MOCK === '1' || import.meta.env.VITE_USE_MOCK === 'true';

export interface ConfirmedFeed {
  scan_id?: string;
  scan_timestamp_utc?: string;
  generated_at_utc?: string;
  count: number;
  long: CandidateRow[];
  short: CandidateRow[];
  symbols: { long: string[]; short: string[] };
  state_counts?: Partial<Record<ScreenerState, number>>;
  occupancy?: ScanMeta['occupancy'];
  daily_unique?: ScanMeta['daily_unique'];
  control?: ScanMeta['control'];
  dmr?: ScanMeta['dmr'];
  alerts?: string[];
  parameter_version?: string;
  dmr_messages?: unknown[];
  api_source?: string;
}

export type ScreenerSseHandler = (event: {
  type: string;
  data: Record<string, unknown>;
}) => void;

export interface ScreenerMetaLite {
  scan_id?: string;
  scan_timestamp_utc?: string;
  parameter_version?: string;
  board_key?: string;
  state_counts?: Partial<Record<ScreenerState, number>>;
  occupancy?: ScanMeta['occupancy'];
  daily_unique?: ScanMeta['daily_unique'];
  confirmed_count?: number;
  long_count?: number;
  short_count?: number;
}

/**
 * 板面前缀。`main` → `/screener`，`y` → `/screener-y`。
 *
 * 「选币榜Y」与「选币榜」共用**同一批取数函数**：换的只是这个前缀。所以两边
 * 的字段、格式、归一化、SSE 事件语义天然一致 —— 不存在第二套解析可以跑偏。
 */
function prefixOf(board: BoardKey = DEFAULT_BOARD): string {
  return boardConfig(board).apiPrefix;
}

function normalizeSnapshot(raw: ScreenerSnapshot): ScreenerSnapshot {
  const fixRow = (r: CandidateRow): CandidateRow => ({
    ...r,
    state: normalizeScreenerState(r.state as unknown as string),
  });
  const fixTr = (t: TransitionEvent): TransitionEvent => ({
    ...t,
    from_state: normalizeScreenerState(t.from_state as unknown as string),
    to_state: normalizeScreenerState(t.to_state as unknown as string),
  });
  const counts: Partial<Record<ScreenerState, number>> = {};
  for (const [k, v] of Object.entries(raw.meta.state_counts || {})) {
    const nk = normalizeScreenerState(k);
    counts[nk] = (counts[nk] || 0) + (v || 0);
  }
  return {
    ...raw,
    meta: { ...raw.meta, state_counts: counts },
    long_pool: raw.long_pool.map(fixRow),
    short_pool: raw.short_pool.map(fixRow),
    transitions: (raw.transitions || []).map(fixTr),
  };
}

async function httpGet<T>(
  path: string,
  opts?: { timeoutMs?: number; retries?: number },
): Promise<T> {
  const url = path.startsWith('http')
    ? path
    : `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`;
  const timeoutMs = opts?.timeoutMs ?? 20_000;
  const retries = Math.max(0, opts?.retries ?? 0);
  let lastErr: Error | null = null;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetch(url, {
        headers: { Accept: 'application/json' },
        signal: ctrl.signal,
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(
          `HTTP ${res.status} ${url}${text ? `: ${text.slice(0, 200)}` : ''}`,
        );
      }
      return (await res.json()) as T;
    } catch (err) {
      const e = err as Error;
      lastErr = e.name === 'AbortError' ? new Error(`请求超时 ${url}`) : e;
    } finally {
      clearTimeout(timer);
    }
  }
  throw lastErr ?? new Error(`请求失败 ${url}`);
}

function delay(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

export async function fetchScreenerLatest(
  board: BoardKey = DEFAULT_BOARD,
): Promise<ScreenerSnapshot> {
  if (USE_MOCK) {
    await delay(80);
    return normalizeSnapshot(generateSnapshot({ seed: 42, poolSize: 160 }));
  }
  try {
    const snap = await httpGet<ScreenerSnapshot>(`${prefixOf(board)}/latest`, {
      timeoutMs: 12_000,
      retries: 2,
    });
    return normalizeSnapshot(snap);
  } catch (err) {
    if (import.meta.env.DEV) {
      console.warn('[screener] gateway failed, using local mock', err);
      return normalizeSnapshot(generateSnapshot({ seed: 42, poolSize: 160 }));
    }
    throw err;
  }
}

export async function fetchScreenerMeta(
  board: BoardKey = DEFAULT_BOARD,
): Promise<ScreenerMetaLite> {
  if (USE_MOCK) {
    const snap = await fetchScreenerLatest(board);
    return {
      scan_id: snap.meta.scan_id,
      scan_timestamp_utc: snap.meta.scan_timestamp_utc,
      parameter_version: snap.meta.parameter_version,
      board_key: board,
      state_counts: snap.meta.state_counts,
      occupancy: snap.meta.occupancy,
      daily_unique: snap.meta.daily_unique,
      confirmed_count: snap.meta.state_counts?.CONFIRMED || 0,
      long_count: snap.long_pool.length,
      short_count: snap.short_pool.length,
    };
  }
  return httpGet<ScreenerMetaLite>(`${prefixOf(board)}/meta`);
}

export async function fetchConfirmedFeed(
  board: BoardKey = DEFAULT_BOARD,
): Promise<ConfirmedFeed> {
  if (USE_MOCK) {
    const snap = await fetchScreenerLatest(board);
    const long = snap.long_pool.filter((r) => r.state === 'CONFIRMED');
    const short = snap.short_pool.filter((r) => r.state === 'CONFIRMED');
    return {
      scan_id: snap.meta.scan_id,
      count: long.length + short.length,
      long,
      short,
      symbols: {
        long: long.map((r) => r.symbol),
        short: short.map((r) => r.symbol),
      },
      state_counts: snap.meta.state_counts,
      occupancy: snap.meta.occupancy,
      daily_unique: snap.meta.daily_unique,
      control: snap.meta.control,
      dmr: snap.meta.dmr,
      alerts: snap.meta.alerts,
      parameter_version: snap.meta.parameter_version,
    };
  }
  const raw = await httpGet<ConfirmedFeed>(`${prefixOf(board)}/confirmed`);
  const fix = (rows: CandidateRow[] = []) =>
    rows.map((r) => ({
      ...r,
      state: normalizeScreenerState(r.state as unknown as string),
    }));
  return {
    ...raw,
    long: fix(raw.long),
    short: fix(raw.short),
    count: raw.count ?? (raw.long?.length || 0) + (raw.short?.length || 0),
  };
}

/**
 * Subscribe to gateway SSE (`/screener/events`).
 * Falls back to polling `/meta` every `pollMs` if EventSource fails.
 * Hidden tabs close the EventSource so HTTP/1.1's 6-per-host slots stay free
 * for `/latest` (public 选币榜 timeout was this queue, not a dead gateway).
 */
export function subscribeScreenerEvents(
  onEvent: ScreenerSseHandler,
  opts?: { pollMs?: number; board?: BoardKey },
): () => void {
  const pollMs = opts?.pollMs ?? 15_000;
  const board = opts?.board ?? DEFAULT_BOARD;
  let closed = false;
  let es: EventSource | null = null;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let lastScan = '';
  let visHandler: (() => void) | null = null;

  const startPoll = () => {
    if (pollTimer || closed) return;
    pollTimer = setInterval(async () => {
      try {
        const meta = await fetchScreenerMeta(board);
        const sid = meta.scan_id || '';
        if (sid && sid !== lastScan) {
          lastScan = sid;
          onEvent({
            type: 'screener.updated',
            data: {
              scan_id: sid,
              confirmed_count: meta.confirmed_count,
              state_counts: meta.state_counts,
              poll: true,
            },
          });
        }
      } catch {
        /* ignore poll errors */
      }
    }, pollMs);
  };

  const stopEs = () => {
    if (es) {
      es.close();
      es = null;
    }
  };

  const connectEs = () => {
    if (closed || USE_MOCK || typeof EventSource === 'undefined') return;
    if (typeof document !== 'undefined' && document.hidden) return;
    if (es) return;
    try {
      const url = `${API_BASE}${prefixOf(board)}/events`;
      es = new EventSource(url);
      const handle = (type: string) => (ev: MessageEvent) => {
        try {
          const data = JSON.parse(ev.data || '{}') as Record<string, unknown>;
          onEvent({ type, data });
        } catch {
          onEvent({ type, data: { raw: ev.data } });
        }
      };
      es.addEventListener('hello', handle('hello'));
      es.addEventListener('screener.updated', handle('screener.updated'));
      es.addEventListener('confirmed.changed', handle('confirmed.changed'));
      es.onerror = () => {
        stopEs();
        startPoll();
      };
    } catch {
      startPoll();
    }
  };

  if (USE_MOCK || typeof EventSource === 'undefined') {
    startPoll();
  } else {
    connectEs();
    if (typeof document !== 'undefined') {
      visHandler = () => {
        if (closed) return;
        if (document.hidden) {
          stopEs();
          startPoll();
        } else {
          connectEs();
        }
      };
      document.addEventListener('visibilitychange', visHandler);
    }
  }

  return () => {
    closed = true;
    stopEs();
    if (pollTimer) clearInterval(pollTimer);
    if (visHandler && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', visHandler);
    }
  };
}

export async function fetchSymbolDetail(
  symbol: string,
  direction: Direction = 'up',
  board: BoardKey = DEFAULT_BOARD,
): Promise<SymbolScreenerDetail> {
  if (USE_MOCK) {
    return mockSymbolDetail(symbol, direction, board);
  }
  try {
    return await httpGet<SymbolScreenerDetail>(
      `${prefixOf(board)}/symbols/${encodeURIComponent(symbol)}?direction=${direction}`,
    );
  } catch (err) {
    if (import.meta.env.DEV) {
      console.warn('[screener] detail gateway failed, mock', err);
      return mockSymbolDetail(symbol, direction, board);
    }
    throw err;
  }
}

export interface KlineResponse {
  bars: OhlcvBar[];
  source: string;
}

export async function fetchKlines(
  symbol: string,
  interval: KlineInterval = '15m',
  limit = 120,
): Promise<KlineResponse> {
  if (USE_MOCK) {
    return { bars: generateMockKlines(symbol, interval, limit), source: 'mock' };
  }
  try {
    const body = await httpGet<{ bars: OhlcvBar[]; source?: string }>(
      `/market/${encodeURIComponent(symbol)}/klines?interval=${interval}&limit=${limit}`,
    );
    return { bars: body.bars || [], source: body.source || 'live' };
  } catch {
    return { bars: generateMockKlines(symbol, interval, limit), source: 'mock' };
  }
}

async function mockSymbolDetail(
  symbol: string,
  direction: Direction,
  board: BoardKey = DEFAULT_BOARD,
): Promise<SymbolScreenerDetail> {
  const snap = await fetchScreenerLatest(board);
  const pool = direction === 'up' ? snap.long_pool : snap.short_pool;
  const current =
    pool.find((r) => r.symbol === symbol.toUpperCase()) ||
    pool[0] ||
    null;
  return {
    current,
    path: [],
    meta: snap.meta,
  } as SymbolScreenerDetail;
}
