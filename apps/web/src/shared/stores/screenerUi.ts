import { create } from 'zustand';
import type { BoardKey } from '../config/boards';
import type { Direction, LiquidityGrade, ScreenerState } from '../types/screener';
import { DEFAULT_POOL_STATES } from '../types/screener';

export type ColumnPreset = 'compact' | 'trader' | 'full_s37';

export type SortId =
  | 'score'
  | 'rank'
  | 'ret_1h'
  | 'ret_4h'
  | 'ret_24h'
  // 24h 与 锚点以来 之间的两列
  | 'ret_1w'
  | 'ret_1mo'
  | 'ret_anchor'
  | 'dir_conf'
  | 'grade'
  | 'pnl_pct'
  | 'pnl_sign'
  | 'liquidity'
  | 'ss'
  // 周期流通市值三列（值取自 shared/lib/mcapGrade.ts 的 MCAP_SORT_ID）
  | 'mcap_30m'
  | 'mcap_2h'
  | 'mcap_6h'
  // 216 天花板四列（y-v2.0.0-r3）：组合 / 方向 Z / 共振 K / 天花板
  | 'mcap_combo'
  | 'mcap_z'
  | 'mcap_k'
  | 'mcap_ceiling';

interface ScreenerUiState {
  direction: Direction;
  dmrActive: boolean;
  activeStates: ScreenerState[];
  columnPreset: ColumnPreset;
  search: string;
  grades: LiquidityGrade[];
  sortId: SortId;
  sortDesc: boolean;
  setDirection: (d: Direction) => void;
  setDmrActive: (v: boolean) => void;
  toggleDmr: () => void;
  setActiveStates: (s: ScreenerState[]) => void;
  toggleState: (s: ScreenerState) => void;
  setColumnPreset: (p: ColumnPreset) => void;
  setSearch: (q: string) => void;
  setGrades: (g: LiquidityGrade[]) => void;
  setSort: (id: SortId) => void;
  resetFilters: () => void;
}

const defaultFilterState = () => ({
  direction: 'up' as Direction,
  dmrActive: false,
  activeStates: [...DEFAULT_POOL_STATES],
  columnPreset: 'compact' as ColumnPreset,
  search: '',
  grades: [] as LiquidityGrade[],
  sortId: 'score' as SortId,
  sortDesc: true,
});

/**
 * Route-lifetime UI store. It survives Screener -> Market -> Back because the
 * Zustand module remains mounted in the SPA, but intentionally does NOT persist
 * into sessionStorage/localStorage. A reload or another device/origin therefore
 * starts from the same complete default view instead of inheriting a stale,
 * device-local filter that can make 525 rows look like one missing-data row.
 *
 * 一块板面一个实例（工厂而不是单例）。「选币榜」与「选币榜Y」的筛选、排序、
 * 方向、列预设必须各管各的 —— 共用一个 store 会让在 Y 上点的分区悄悄改掉
 * 选币榜的视图，那是最难查的一类串台。
 */
export const createScreenerUiStore = () =>
  create<ScreenerUiState>((set, get) => ({
    ...defaultFilterState(),
    setDirection: (direction) => set({ direction }),
    setDmrActive: (dmrActive) => set({ dmrActive }),
    toggleDmr: () => {
      const cur = get();
      if (cur.dmrActive && cur.activeStates.length === 0) {
        set({ dmrActive: false, activeStates: ['CONFIRMED'] });
      } else {
        set({ dmrActive: !cur.dmrActive });
      }
    },
    setActiveStates: (activeStates) => set({ activeStates }),
    toggleState: (s) => {
      const cur = get();
      if (cur.activeStates.includes(s)) {
        if (cur.activeStates.length === 1 && !cur.dmrActive) return;
        set({ activeStates: cur.activeStates.filter((x) => x !== s) });
      } else {
        set({ activeStates: [...cur.activeStates, s] });
      }
    },
    setColumnPreset: (columnPreset) => set({ columnPreset }),
    setSearch: (search) => set({ search }),
    setGrades: (grades) => set({ grades }),
    setSort: (sortId) => {
      const cur = get();
      if (cur.sortId === sortId) set({ sortDesc: !cur.sortDesc });
      else set({ sortId, sortDesc: true });
    },
    resetFilters: () => set(defaultFilterState()),
  }));

/**
 * 「选币榜」的 UI 仓库。名字与导出位置保持不变 —— 既有 import 一行都不用改。
 * 参数版本 param-v1.4.0-staircase-confirm-dmr。
 */
export const useScreenerUi = createScreenerUiStore();
// X v1.3.0 复刻 main v1.4.0，但 UI/复盘独立；未来只改 X overrides。
export const useScreenerXUi = createScreenerUiStore();

/**
 * 「选币榜Y」的 UI 仓库（参数版本 param-v2.0.0-screener-y）。
 * 与上面**同一份实现**，只是各持一份状态。
 */
export const useScreenerYUi = createScreenerUiStore();

export type ScreenerUiStore = typeof useScreenerUi;

/** 按板面取仓库。页面组件只调这一个函数，不直接引用具体实例。 */
export function screenerUiStoreFor(board: BoardKey): ScreenerUiStore {
  return { main: useScreenerUi, x: useScreenerXUi, y: useScreenerYUi }[board];
}
