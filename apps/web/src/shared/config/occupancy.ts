/** 确认区去重占用的展示/告警目标带。选币榜与选币榜Y 共用同一套。
 *
 * 不进选币谓词，只驱动页头文案、颜色，以及服务端 CONFIRM_UNDERFILLED /
 * CONFIRM_OVERFLOW。须与 `StateConfig.occupancy_lo/hi` 保持一致。
 */
export const CONFIRMED_OCCUPANCY_LO = 10;
export const CONFIRMED_OCCUPANCY_HI = 40;
export const CONFIRMED_OCCUPANCY_LABEL = `${CONFIRMED_OCCUPANCY_LO}–${CONFIRMED_OCCUPANCY_HI}`;
