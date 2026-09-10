/**
 * 复盘筛选条件 ⇄ URL query string。
 *
 * 让 `/review?zones=DMR,CONFIRMED&range=7&basis=exit&sort=pnl_pct&desc=1` 可分享、
 * 可收藏、可在 issue 里贴出来复现。仅时间控件按账本记忆在 localStorage；
 * 其他筛选仍只在 URL。显式 URL 时间优先于本地记忆。
 */
import { BOARDS, DEFAULT_BOARD, type BoardKey } from '../../shared/config/boards';
import { REVIEW_SORT_KEYS, REVIEW_ZONES } from '../../shared/types/review';
import type { ReviewSortId, ReviewZone } from '../../shared/types/review';
import type {
  ReviewAttribution,
  ReviewColumnPreset,
  ReviewDir,
  ReviewPeriod,
  ReviewRange,
  ReviewZoneMode,
} from '../../shared/stores/reviewUi';
import { presetOf } from '../../shared/stores/reviewUi';

export interface ReviewUrlState {
  /** 规则版本 / 账本：`main` = v1.4.0 选币榜，`y` = v2.0.0 选币榜Y */
  board: BoardKey;
  zones: ReviewZone[];
  direction: ReviewDir;
  attribution: ReviewAttribution;
  zoneMode: ReviewZoneMode;
  rangeDays: ReviewRange;
  useCustom: boolean;
  customFrom: string;
  customTo: string;
  comparePeriods: ReviewPeriod[];
  compareOn: boolean;
  search: string;
  includeOpen: boolean;
  onlyFlagged: boolean;
  excludeCycleReset: boolean;
  wholeCycles: boolean;
  minDwellNodes: number;
  columnPreset: ReviewColumnPreset;
  paramVersion: string;
  /** 调参段（param_hash）。'all' = 不过滤；'null' = 只看没有指纹的历史行。 */
  paramHash: string;
  sortId: ReviewSortId;
  sortDesc: boolean;
}

const RANGES: ReviewRange[] = [1, 7, 14, 28];

export function stateToSearch(s: ReviewUrlState): string {
  const p = new URLSearchParams();
  // 默认板面不写进 URL —— 既有的 /review?zones=... 链接保持原样可用。
  if (s.board && s.board !== DEFAULT_BOARD) p.set('board', s.board);
  p.set('zones', s.zones.join(','));
  if (s.direction !== 'both') p.set('pool', s.direction);
  if (s.attribution !== 'exit') p.set('basis', s.attribution);
  if (s.zoneMode !== 'parallel') p.set('mode', s.zoneMode);
  if (s.useCustom && s.customFrom && s.customTo) {
    p.set('from', s.customFrom);
    p.set('to', s.customTo);
  } else if (s.rangeDays !== 7) {
    p.set('range', String(s.rangeDays));
  }
  if (s.compareOn) {
    const cmp = s.comparePeriods.filter((x) => x.from && x.to).map((x) => `${x.from}_${x.to}`);
    if (cmp.length) p.set('cmp', cmp.join(';'));
  }
  if (s.search.trim()) p.set('coins', s.search.trim());
  if (s.includeOpen) p.set('open', '1');
  if (s.onlyFlagged) p.set('flagged', '1');
  if (s.excludeCycleReset) p.set('nocycle', '1');
  // 选币榜Y 默认 nocycle=1；用户显式关掉时要把 0 写进 URL，否则往返会再被默认勾上。
  // X v1.3.0 复刻 main v1.4.0 的无周期复盘；未来只经 X overrides 演进。
  if (BOARDS[s.board]?.cycleEnabled && !s.excludeCycleReset) p.set('nocycle', '0');
  if (s.wholeCycles) p.set('whole', '1');
  if (s.minDwellNodes) p.set('dwell', String(s.minDwellNodes));
  if (s.columnPreset !== 'full') p.set('cols', s.columnPreset);
  if (s.paramVersion && s.paramVersion !== 'all') p.set('pv', s.paramVersion);
  if (s.paramHash && s.paramHash !== 'all') p.set('ph', s.paramHash);
  if (s.sortId !== 'enter_time_utc') p.set('sort', s.sortId);
  if (s.sortDesc) p.set('desc', '1');
  return p.toString();
}

/** URL → 部分状态。只接受能识别的值，其余保持 store 默认，绝不抛错。 */
export function searchToState(search: string): Partial<ReviewUrlState> & { preset?: string } {
  const p = new URLSearchParams(search);
  const out: Partial<ReviewUrlState> & { preset?: string } = {};

  const board = p.get('board');
  if (board && board in BOARDS) out.board = board as BoardKey;

  const zonesRaw = p.get('zones');
  if (zonesRaw) {
    const zones = zonesRaw
      .split(',')
      .map((z) => z.trim().toUpperCase())
      .filter((z): z is ReviewZone => (REVIEW_ZONES as string[]).includes(z));
    if (zones.length) {
      const ordered = REVIEW_ZONES.filter((z) => zones.includes(z));
      out.zones = ordered;
      out.preset = presetOf(ordered);
    }
  }
  const pool = p.get('pool');
  if (pool === 'up' || pool === 'down' || pool === 'both') out.direction = pool;
  const basis = p.get('basis');
  if (basis === 'exit' || basis === 'enter' || basis === 'contained') out.attribution = basis;
  const mode = p.get('mode');
  if (mode === 'parallel' || mode === 'merge') out.zoneMode = mode;

  const from = p.get('from');
  const to = p.get('to');
  if (from && to) {
    out.customFrom = from;
    out.customTo = to;
    out.useCustom = true;
  } else {
    const r = Number(p.get('range'));
    if (RANGES.includes(r as ReviewRange)) {
      out.rangeDays = r as ReviewRange;
      out.useCustom = false;
    }
  }

  const cmp = p.get('cmp');
  if (cmp) {
    const periods = cmp
      .split(';')
      .map((chunk, i) => {
        const [a, b] = chunk.split('_');
        return a && b ? { id: `u${i}`, from: a, to: b } : null;
      })
      .filter((x): x is ReviewPeriod => x !== null)
      .slice(0, 3);
    if (periods.length) {
      out.comparePeriods = periods;
      out.compareOn = true;
    }
  }

  const coins = p.get('coins');
  if (coins) out.search = coins;
  if (p.get('open') === '1') out.includeOpen = true;
  if (p.get('flagged') === '1') out.onlyFlagged = true;
  if (p.get('nocycle') === '1') out.excludeCycleReset = true;
  if (p.get('nocycle') === '0') out.excludeCycleReset = false;
  // To-Be：切到 Y 且 URL 没写 nocycle 时默认剔除周期强平。主板行为不变。
  if (out.board && BOARDS[out.board].cycleEnabled && p.get('nocycle') == null) out.excludeCycleReset = true;
  if (p.get('whole') === '1') out.wholeCycles = true;
  const dwell = Number(p.get('dwell'));
  if (Number.isFinite(dwell) && dwell > 0) out.minDwellNodes = Math.floor(dwell);
  const pv = p.get('pv');
  if (pv) out.paramVersion = pv;
  const ph = p.get('ph');
  if (ph) out.paramHash = ph;
  // 入点锁属于某一本账本。URL 若带着另一块板面的当前身份
  // （例如 /review?board=y&pv=param-v1.4.0-staircase-confirm-dmr），
  // AND 到 Y 账本上会得到 0 笔完整交易，且 Y 只有一个参数版本时
  // ParamVersionBar 被藏掉，页面只剩空白。主板自己的历史入点
  // （v1.2 / v1.3 dual-path）必须保留。
  if (out.paramVersion && out.paramVersion !== 'all') {
    const board = out.board ?? DEFAULT_BOARD;
    const foreign = new Set(
      Object.values(BOARDS)
        .filter((b) => b.key !== board)
        .map((b) => b.parameterVersion),
    );
    if (foreign.has(out.paramVersion)) out.paramVersion = 'all';
  }
  const cols = p.get('cols');
  if (cols === 'compact' || cols === 'trader' || cols === 'full') out.columnPreset = cols;
  const sort = p.get('sort');
  if (sort && (REVIEW_SORT_KEYS as readonly string[]).includes(sort)) out.sortId = sort as ReviewSortId;
  if (p.get('desc') === '1') out.sortDesc = true;
  return out;
}
