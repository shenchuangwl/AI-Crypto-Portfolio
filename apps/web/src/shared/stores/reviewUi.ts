import { create } from 'zustand';
import { BOARDS, DEFAULT_BOARD, type BoardKey } from '../config/boards';
import type { Direction, ReviewSortId, ReviewZone } from '../types/review';
import { REVIEW_ZONES } from '../types/review';
import { defaultReviewRange } from '../lib/reviewTime';
import { readReviewTime, writeReviewTime } from '../lib/reviewTimePreferences';

export type ReviewRange = 1 | 7 | 14 | 28;
export type ReviewDir = Direction | 'both';
export type ReviewAttribution = 'exit' | 'enter' | 'contained';
export type ReviewPreset = 'executable' | 'funnel' | 'control' | 'custom';
export type ReviewZoneMode = 'parallel' | 'merge';
export type ReviewColumnPreset = 'compact' | 'trader' | 'full';

/** 自定义对比周期。主周期之外最多 3 个，合计 4 列（§11.2）。 */
export interface ReviewPeriod {
  id: string;
  from: string;
  to: string;
  label?: string;
}

export const MAX_COMPARE_PERIODS = 3;

const EXECUTABLE: ReviewZone[] = ['DMR', 'CONFIRMED'];
const FUNNEL: ReviewZone[] = ['DMR', 'CONFIRMED', 'QUALIFIED', 'WATCH'];
const CONTROL: ReviewZone[] = ['ELIMINATED', 'DATA_INSUFFICIENT', 'LOW_CONFIDENCE'];

const PRESET_ZONES: Record<Exclude<ReviewPreset, 'custom'>, ReviewZone[]> = {
  executable: EXECUTABLE,
  funnel: FUNNEL,
  control: CONTROL,
};

function sameZones(a: ReviewZone[], b: ReviewZone[]): boolean {
  return a.length === b.length && a.every((z) => b.includes(z));
}

/** 选中集合回落到预设名；手改后不再谎称仍是「可执行区」。 */
export function presetOf(zones: ReviewZone[]): ReviewPreset {
  for (const [name, list] of Object.entries(PRESET_ZONES)) {
    if (sameZones(zones, list)) return name as ReviewPreset;
  }
  return 'custom';
}

interface ReviewUi {
  /**
   * 规则版本（= 板面账本）：`main` 选币榜 v1.4.0 / `y` 选币榜Y v2.0.0。
   *
   * 与 `paramVersion` 是两件事，别混：
   *   - `board`        选**哪一本账本**（哪套选币机制产出的交易）
   *   - `paramVersion` 在同一本账本里，只看某一段参数版本的成绩
   * v1.4.0 与 v2.0.0 各有各的 sqlite，所以切规则版本切的是 `board`。
   */
  board: BoardKey;
  rangeDays: ReviewRange;
  customFrom: string;
  customTo: string;
  useCustom: boolean;
  comparePeriods: ReviewPeriod[];
  compareOn: boolean;
  attribution: ReviewAttribution;
  direction: ReviewDir;
  zones: ReviewZone[];
  zoneMode: ReviewZoneMode;
  preset: ReviewPreset;
  search: string;
  includeOpen: boolean;
  onlyFlagged: boolean;
  /** 排除周期重置强平（只对启用了 24h 周期的规则版本有意义，如 v2.0.0） */
  excludeCycleReset: boolean;
  /**
   * 只算已经跑完的周期，排除进行中的当前周期。
   *
   * 进行中的周期只跑了几个小时，把它和完整的 24 小时周期一起平均，
   * 分母口径就不一样了 —— 想做周期间对比时应当打开。
   * 只对有时间区的规则版本（v2.0.0）有意义。
   */
  wholeCycles: boolean;
  minDwellNodes: number;
  columnPreset: ReviewColumnPreset;
  /** 'all' = 不按参数版本过滤 */
  paramVersion: string;
  /**
   * 'all' = 不按调参段过滤；'null' = 只看没有指纹的历史行。
   *
   * 与 `paramVersion` 是两层：版本管「哪套标准」，指纹管「这套标准的哪一次调参」。
   * 选币榜Y 的 216 主导层上线前后版本号相同、只有指纹变，不按它分段就会混算。
   */
  paramHash: string;
  sortId: ReviewSortId;
  sortDesc: boolean;
  setRange: (d: ReviewRange) => void;
  setCustom: (from: string, to: string) => void;
  clearCustom: () => void;
  addComparePeriod: (p?: Partial<ReviewPeriod>) => void;
  updateComparePeriod: (id: string, patch: Partial<ReviewPeriod>) => void;
  removeComparePeriod: (id: string) => void;
  toggleCompare: () => void;
  setAttribution: (a: ReviewAttribution) => void;
  setDirection: (d: ReviewDir) => void;
  toggleZone: (z: ReviewZone) => void;
  setZones: (z: ReviewZone[]) => void;
  setPreset: (p: ReviewPreset) => void;
  setZoneMode: (m: ReviewZoneMode) => void;
  setSearch: (q: string) => void;
  setIncludeOpen: (v: boolean) => void;
  setOnlyFlagged: (v: boolean) => void;
  setExcludeCycleReset: (v: boolean) => void;
  setWholeCycles: (v: boolean) => void;
  setMinDwell: (n: number) => void;
  setColumnPreset: (p: ReviewColumnPreset) => void;
  /** 切规则版本 = 换账本；清参数锁，恢复该账本独立记忆的时间设置。 */
  setBoard: (b: BoardKey) => void;
  setParamVersion: (v: string) => void;
  setParamHash: (v: string) => void;
  lockParamHash: (v: string, from?: string | null, to?: string | null) => void;
  /** 锁定版本并把窗口对齐到该版本自己的数据区间；`all` 则解锁并恢复原周期。 */
  lockParamVersion: (v: string, from?: string | null, to?: string | null) => void;
  setSort: (id: ReviewSortId) => void;
  hydrate: (patch: Partial<ReviewUi>) => void;
  reset: () => void;
}

const defaults = () => ({
  board: DEFAULT_BOARD as BoardKey,
  rangeDays: 7 as ReviewRange,
  customFrom: defaultReviewRange().from,
  customTo: defaultReviewRange().to,
  useCustom: false,
  comparePeriods: [] as ReviewPeriod[],
  compareOn: false,
  attribution: 'exit' as ReviewAttribution,
  direction: 'both' as ReviewDir,
  zones: [...EXECUTABLE] as ReviewZone[],
  zoneMode: 'parallel' as ReviewZoneMode,
  preset: 'executable' as ReviewPreset,
  search: '',
  includeOpen: false,
  onlyFlagged: false,
  excludeCycleReset: false,
  wholeCycles: false,
  minDwellNodes: 0,
  columnPreset: 'full' as ReviewColumnPreset,
  paramVersion: 'all',
  paramHash: 'all',
  sortId: 'enter_time_utc' as ReviewSortId,
  sortDesc: false,
});

let periodSeq = 0;

export const useReviewUi = create<ReviewUi>((set, get) => ({
  ...defaults(),
  ...readReviewTime(DEFAULT_BOARD),
  setRange: (rangeDays) => {
    set({ rangeDays, useCustom: false });
    writeReviewTime(get().board, get());
  },
  // Both ends required: a half-filled range would send `from` with the window
  // end silently falling back to the watermark, quietly changing the span.
  setCustom: (customFrom, customTo) => {
    set({ customFrom, customTo, useCustom: Boolean(customFrom && customTo) });
    writeReviewTime(get().board, get());
  },
  clearCustom: () => {
    set({ useCustom: false, customFrom: '', customTo: '' });
    writeReviewTime(get().board, get());
  },
  addComparePeriod: (p) => {
    const cur = get();
    if (cur.comparePeriods.length >= MAX_COMPARE_PERIODS) return;
    do { periodSeq += 1; } while (cur.comparePeriods.some((p) => p.id === `p${periodSeq}`));
    const range = defaultReviewRange(cur.rangeDays);
    set({
      compareOn: true,
      comparePeriods: [
        ...cur.comparePeriods,
        { id: `p${periodSeq}`, from: p?.from ?? range.from, to: p?.to ?? range.to, label: p?.label },
      ],
    });
    writeReviewTime(get().board, get());
  },
  updateComparePeriod: (id, patch) => {
    set({
      comparePeriods: get().comparePeriods.map((x) => (x.id === id ? { ...x, ...patch } : x)),
    });
    writeReviewTime(get().board, get());
  },
  removeComparePeriod: (id) => {
    const left = get().comparePeriods.filter((x) => x.id !== id);
    set({ comparePeriods: left, compareOn: left.length > 0 });
    writeReviewTime(get().board, get());
  },
  toggleCompare: () => {
    const cur = get();
    if (!cur.compareOn && cur.comparePeriods.length === 0) {
      cur.addComparePeriod();
      return;
    }
    set({ compareOn: !cur.compareOn });
    writeReviewTime(get().board, get());
  },
  setAttribution: (attribution) => set({ attribution }),
  setDirection: (direction) => set({ direction }),
  toggleZone: (z) => {
    const cur = get();
    const on = cur.zones.includes(z);
    if (on && cur.zones.length === 1) return; // 至少保留一枚（复刻选币榜）
    const zones = on ? cur.zones.filter((x) => x !== z) : [...cur.zones, z];
    // 保持 REVIEW_ZONES 的固定序，避免 chip 顺序随点击漂移
    const ordered = REVIEW_ZONES.filter((x) => zones.includes(x));
    set({ zones: ordered, preset: presetOf(ordered) });
  },
  setZones: (z) => {
    const ordered = REVIEW_ZONES.filter((x) => z.includes(x));
    const zones = ordered.length ? ordered : [...EXECUTABLE];
    set({ zones, preset: presetOf(zones) });
  },
  setPreset: (preset) => {
    if (preset === 'custom') return; // 'custom' 是结果不是动作
    const zones = PRESET_ZONES[preset];
    set({
      preset,
      zones: [...zones],
      zoneMode: preset === 'control' ? 'merge' : 'parallel',
    });
  },
  setZoneMode: (zoneMode) => set({ zoneMode }),
  setSearch: (search) => set({ search }),
  setIncludeOpen: (includeOpen) => set({ includeOpen }),
  setOnlyFlagged: (onlyFlagged) => set({ onlyFlagged }),
  setExcludeCycleReset: (excludeCycleReset) => set({ excludeCycleReset }),
  setWholeCycles: (wholeCycles) => set({ wholeCycles }),
  setMinDwell: (minDwellNodes) => set({ minDwellNodes }),
  setColumnPreset: (columnPreset) => set({ columnPreset }),
  setBoard: (board) => {
    if (get().board === board) return;
    set({
      board,
      // 各板面自己的时间记忆必须保留；入点锁则不能跟着走。
      ...readReviewTime(board),
      paramVersion: 'all',
      paramHash: 'all',
      // To-Be：board=y 默认剔除 CYCLE_RESET；主板没有该旗标，保持 false。
      // X v1.3.0 复刻 main v1.4.0 的无周期复盘；能力镜像，演进只经 X overrides。
      excludeCycleReset: BOARDS[board].cycleEnabled,
      wholeCycles: false,
    });
  },
  setParamVersion: (paramVersion) => set({ paramVersion }),
  setParamHash: (paramHash) => set({ paramHash }),
  lockParamHash: (paramHash, from, to) => {
    // 与 lockParamVersion 同理：每个调参段只在一段有限时间里生效过，
    // 不把窗口一起对齐就会切出一片空白。
    if (paramHash === 'all') {
      set({ paramHash, useCustom: false, customFrom: '', customTo: '' });
      return;
    }
    if (from && to) {
      set({ paramHash, customFrom: from, customTo: to, useCustom: true });
      return;
    }
    set({ paramHash });
  },
  lockParamVersion: (paramVersion, from, to) => {
    // 版本筛选与时间窗口是两个独立条件，会被 AND 到一起。每个参数版本只在一段
    // 有限的时间里生效过（v1.2.0 的可执行区交易 8.21 就停了），而窗口默认锚在
    // 「现在」——两者不重叠时页面会变成一片空白，看上去像是数据丢了。
    // 所以锁定版本时顺带把窗口对齐到它自己的数据区间：点哪个版本就看得到哪个版本。
    if (paramVersion === 'all') {
      set({ paramVersion, useCustom: false, customFrom: '', customTo: '' });
      return;
    }
    if (from && to) {
      set({ paramVersion, customFrom: from, customTo: to, useCustom: true });
      return;
    }
    set({ paramVersion });
  },
  setSort: (sortId) => {
    const cur = get();
    if (cur.sortId === sortId) set({ sortDesc: !cur.sortDesc });
    else set({ sortId, sortDesc: sortId !== 'enter_time_utc' && sortId !== 'symbol' });
  },
  hydrate: (patch) => set(patch as Partial<ReviewUi>),
  reset: () => set(defaults()),
}));

export { EXECUTABLE, FUNNEL, CONTROL, REVIEW_ZONES };
