/**
 * 板面变体注册表（前端侧）。
 *
 * 「选币榜」与「选币榜Y」是同一套 UI 的两个实例：同样的列、同样的分区、同样的
 * 排序与筛选、同样的实时刷新机制，只是取数前缀、UI 状态仓库与参数版本不同。
 * 页面组件只认这里的 `BoardConfig`，不认字符串字面量 —— 加第三块板面时，
 * 除了这张表和一条路由，页面代码一行都不用改。
 *
 * ⚠ 服务端的事实源是 `packages/config/board-variants.json`（网关 `/api/v1/boards`
 * 会把它原样吐出来）。这张表是它的前端镜像，改一边必须改另一边；
 * `key` / `apiPrefix` / `route` / `parameterVersion` 四项必须逐字一致。
 */

// 选币榜X v1.3.0 当前复刻 main v1.4.0；独立复盘闭环，未来参数只经 X overrides 演进。
export type BoardKey = 'main' | 'x' | 'y';

export interface BoardConfig {
  key: BoardKey;
  /** 顶栏与页头显示名 */
  label: string;
  /** 页头左上角的小徽标 */
  mark: string;
  /** 前端路由 */
  route: string;
  /** 网关 API 前缀，相对 API_BASE（`/api/v1`） */
  apiPrefix: string;
  /** 该板面对应的参数版本；与后端 board-variants.json 必须一致 */
  parameterVersion: string;
  /** 复盘接口的 `board=` 取值 —— 决定读哪一本账本 */
  reviewBoard: string;
  /** 复盘「规则版本」切换条上的短名 */
  rulesetLabel: string;
  /** 该板面的 DMR 候选是否可被执行层消费（选币榜Y = false，只做展示与复盘） */
  dmrExecutable: boolean;
  /** X 与 main 连续持有，Y 才周期重置；账本/能力镜像，不是第二个调参入口。 */
  dataDir: string;
  cycleEnabled: boolean;
  /** 页头副标题里的一句话说明 */
  note?: string;
}

export const BOARDS: Record<BoardKey, BoardConfig> = {
  main: {
    key: 'main',
    label: '选币榜',
    mark: '§37',
    route: '/screener',
    apiPrefix: '/screener',
    parameterVersion: 'param-v1.4.0-staircase-confirm-dmr',
    reviewBoard: 'main',
    rulesetLabel: 'v1.4.0',
    dmrExecutable: true,
    dataDir: 'data/coin-selection',
    cycleEnabled: false,
  },
  x: {
    key: 'x',
    label: '选币榜X',
    mark: 'X',
    route: '/screener-x',
    apiPrefix: '/screener-x',
    parameterVersion: 'param-v1.3.0-screener-x',
    reviewBoard: 'x',
    rulesetLabel: 'v1.3.0',
    dmrExecutable: false,
    dataDir: 'data/coin-selection-x',
    cycleEnabled: false,
    note: 'v1.3.0 当前复刻 main v1.4.0；216/Z/K/天花板仅 shadow 观察；独立复盘，候选不可执行',
  },
  y: {
    key: 'y',
    label: '选币榜Y',
    mark: 'Y',
    route: '/screener-y',
    apiPrefix: '/screener-y',
    parameterVersion: 'param-v2.0.0-screener-y',
    reviewBoard: 'y',
    rulesetLabel: 'v2.0.0',
    dmrExecutable: false,
    dataDir: 'data/coin-selection-y',
    cycleEnabled: true,
    note: 'v2.0.0 TUNED_V2_MCAP_COMBO：w_mcap=0.25>w_mom=0.20；候选不进执行层',
  },
};

export const BOARD_KEYS: BoardKey[] = ['main', 'x', 'y'];

export const DEFAULT_BOARD: BoardKey = 'main';

export function boardConfig(key: BoardKey | string | undefined): BoardConfig {
  return BOARDS[(key as BoardKey) in BOARDS ? (key as BoardKey) : DEFAULT_BOARD];
}

/**
 * Grid → K 线 round-trip 的 `from=` 标记。
 * `main` 必须仍是 `screener`：选币榜原有 URL（`/market/XXX?from=screener`）一个字节都不能改。
 */
export function originTagFor(board: BoardKey): string {
  return { main: 'screener', x: 'screener-x', y: 'screener-y' }[board];
}

/**
 * 把 K 线页的 `?from=` 还原成板面 key。未知 / 缺省一律回落选币榜，
 * 这样从行情 / 合约清单 / 工作台进来、或直接打开 `/market/:symbol` 时，
 * 详情仍走主板面，行为与改动前一致。
 */
export function boardFromOrigin(from: string | null | undefined): BoardKey {
  const key = (from || '').trim();
  if (key === 'screener-y' || key === 'y') return 'y';
  if (key === 'screener-x' || key === 'x') return 'x';
  return DEFAULT_BOARD;
}

/** 非板面来源（行情 / 工作台 / 复盘 / 合约清单）的返回路由。 */
const ORIGIN_HREF: Record<string, string> = {
  screener: BOARDS.main.route,
  'screener-x': BOARDS.x.route,
  x: BOARDS.x.route,
  'screener-y': BOARDS.y.route,
  y: BOARDS.y.route,
  review: '/review',
  quotes: '/quotes',
  terminal: '/terminal',
  markets: '/markets',
};

/**
 * K 线页「返回榜单」的落地 href。选币榜Y 必须回 `/screener-y`；
 * 缺省 / 未知仍回 `/screener`，选币榜原路径保持不变。
 */
export function backHrefFromOrigin(from: string | null | undefined): string {
  const key = (from || '').trim();
  if (!key) return BOARDS.main.route;
  return ORIGIN_HREF[key] ?? BOARDS.main.route;
}

/** `param-v2.0.0-screener-y` → `v2.0.0`；找不到就原样回退。 */
export function shortParamVersion(v: string): string {
  const m = /v(\d+\.\d+\.\d+)/.exec(v || '');
  return m ? `v${m[1]}` : (v || '').replace(/^param-/, '');
}
