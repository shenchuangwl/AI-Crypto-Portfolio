import { fileURLToPath } from 'node:url';

const here = (p) => fileURLToPath(new URL(p, import.meta.url));

/** 每个验证闸一个自包含 bundle：`<name>.verify.tsx` → `.out/<name>.mjs`。 */
const gate = (name) => ({
  input: here(`${name}.verify.tsx`),
  // react / react-dom / react-router-dom 走 node_modules，zustand 换成 SSR 传输替身
  external: [/^react($|\/)/, /^react-dom($|\/)/, /^react-router-dom($|\/)/, /^node:/],
  resolve: {
    alias: { zustand: here('zustand-ssr-shim.mjs') },
  },
  output: {
    file: here(`.out/${name}.mjs`),
    format: 'esm',
    // 验证闸跑在裸 node 里，没有 Vite 注入的 import.meta.env。补一个空对象，
    // 让 `env.VITE_API_BASE ?? '/api/v1'` 这类回退照常生效，
    // 而不必为测试改动应用代码。
    banner: 'import.meta.env ??= {};',
  },
  platform: 'node',
});

export default [
  gate('mcap-sort'),
  gate('period-returns'),
  gate('pool-counts'),
  gate('review-counts'),
  gate('review-time'),
  gate('board-split'),
  gate('mcap-zone'),
  gate('onlycoin'),
  gate('onlycoin-stats'),
];
