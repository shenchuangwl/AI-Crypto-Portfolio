import type { DeepPartial, Styles } from 'klinecharts';

/** Dark terminal theme aligned with the rest of the USDT-M shell. */
export const KLINE_STYLES: DeepPartial<Styles> = {
  grid: {
    show: true,
    horizontal: { color: '#1a1f27' },
    vertical: { color: '#1a1f27' },
  },
  candle: {
    type: 'candle_solid' as never,
    bar: {
      upColor: '#0ecb81',
      downColor: '#f6465d',
      noChangeColor: '#848e9c',
      upBorderColor: '#0ecb81',
      downBorderColor: '#f6465d',
      noChangeBorderColor: '#848e9c',
      upWickColor: '#0ecb81',
      downWickColor: '#f6465d',
      noChangeWickColor: '#848e9c',
    },
    tooltip: { showRule: 'always' as never, showType: 'standard' as never },
    priceMark: {
      show: true,
      high: { show: true, color: '#848e9c' },
      low: { show: true, color: '#848e9c' },
      last: {
        show: true,
        upColor: '#0ecb81',
        downColor: '#f6465d',
        noChangeColor: '#848e9c',
      },
    },
  },
  indicator: {
    lastValueMark: { show: true },
    tooltip: { showRule: 'always' as never, showType: 'standard' as never },
  },
  xAxis: {
    axisLine: { color: '#2b3139' },
    tickLine: { color: '#2b3139' },
    tickText: { color: '#848e9c' },
  },
  yAxis: {
    axisLine: { color: '#2b3139' },
    tickLine: { color: '#2b3139' },
    tickText: { color: '#848e9c' },
  },
  separator: { color: '#1a1f27' },
  crosshair: {
    show: true,
    horizontal: {
      show: true,
      line: { show: true, color: '#5e6673' },
      text: { show: true },
    },
    vertical: {
      show: true,
      line: { show: true, color: '#5e6673' },
      text: { show: true },
    },
  },
  overlay: {
    line: { color: '#f0b90b', size: 1 },
    point: { color: '#f0b90b', borderColor: '#f0b90b' },
    text: { color: '#eaecef' },
    rect: { color: 'rgba(240,185,11,0.12)', borderColor: '#f0b90b' },
  },
};

export const MAIN_INDICATORS = ['MA', 'EMA', 'BOLL', 'SAR'] as const;
export const SUB_INDICATORS = ['VOL', 'MACD', 'RSI', 'KDJ', 'WR'] as const;

export type MainIndicator = (typeof MAIN_INDICATORS)[number];
export type SubIndicator = (typeof SUB_INDICATORS)[number];

export const DRAW_TOOLS: { id: string; label: string }[] = [
  { id: 'segment', label: '线段' },
  { id: 'straightLine', label: '直线' },
  { id: 'rayLine', label: '射线' },
  { id: 'horizontalStraightLine', label: '水平' },
  { id: 'verticalStraightLine', label: '垂直' },
  { id: 'priceLine', label: '价线' },
  { id: 'priceChannelLine', label: '通道' },
  { id: 'fibonacciLine', label: '斐波那契' },
  { id: 'rect', label: '矩形' },
  { id: 'circle', label: '圆' },
  { id: 'text', label: '文字' },
];
