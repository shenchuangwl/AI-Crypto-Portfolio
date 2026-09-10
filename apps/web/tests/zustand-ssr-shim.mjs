/**
 * 测试替身：只替换 zustand 的「React 传输层」，不替换 store 本身。
 *
 * zustand v5 的 useStore 在服务端渲染时走 useSyncExternalStore 的
 * getServerSnapshot = () => selector(api.getInitialState())，也就是**恒为初始状态**，
 * 所以 renderToStaticMarkup 看不见 setState，表头永远停在默认的 Score 排序上。
 *
 * 本 shim 让 hook 直接同步读 api.getState()（一次性静态渲染不需要订阅），
 * 于是可以对着真实 store 状态断言表头高亮与升降序箭头。
 * createStore / setSort / defaultFilterState 全部仍是产品代码。
 */
import { createStore } from 'zustand/vanilla';

const identity = (x) => x;

export function create(createState) {
  if (!createState) return create;
  const api = createStore(createState);
  const useBoundStore = (selector = identity) => selector(api.getState());
  Object.assign(useBoundStore, api);
  return useBoundStore;
}

export function useStore(api, selector = identity) {
  return selector(api.getState());
}

export default { create, useStore };
