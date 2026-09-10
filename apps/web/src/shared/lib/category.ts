/** Client fallback if ingest has not attached Chinese categories yet. */
const ZH: Record<string, string> = {
  'layer-1': '第一层级',
  'layer1': '第一层级',
  'layer-2': '第二层级',
  'layer2': '第二层级',
  layer1_layer2: '第一层级 / 第二层级',
  pow: '工作量证明',
  pos: '权益证明',
  'mining-zone': '挖矿',
  payments: '付款',
  payment: '付款',
  defi: '去中心化金融',
  infrastructure: '基础设施',
  ai: '人工智能',
  meme: '迷因',
  gaming: '游戏',
  nft: 'NFT',
  metaverse: '元宇宙',
  storage: '存储',
  'storage-zone': '存储',
  rwa: '真实世界资产',
  privacy: '隐私',
  stablecoin: '稳定币',
  alpha: 'Alpha',
  chinese: '中文社区',
};

const SKIP = new Set([
  '',
  'seed',
  'launchpool',
  'launchpad',
  'hodler',
  'monitoring',
  'innovation-zone',
  'newlisting',
  'megadrop',
]);

function zh(raw: string): string | null {
  const s = raw.trim();
  if (!s) return null;
  const k = s.toLowerCase();
  if (SKIP.has(k)) return null;
  return ZH[k] || ZH[k.replace(/_/g, '-')] || s;
}

export function formatCategory(input: {
  category?: string;
  categories?: string[];
  underlying_sub_type?: string | string[];
  spot_tags?: string[];
}): string {
  if (input.category && input.category !== '—') return input.category;
  if (input.categories?.length) return input.categories.join('，');
  const raw: string[] = [];
  const sub = input.underlying_sub_type;
  if (Array.isArray(sub)) raw.push(...sub);
  else if (sub) raw.push(sub);
  if (input.spot_tags) raw.push(...input.spot_tags);
  const seen = new Set<string>();
  const labels: string[] = ['加密货币'];
  seen.add('加密货币');
  for (const r of raw) {
    const lab = zh(r);
    if (!lab || seen.has(lab)) continue;
    seen.add(lab);
    labels.push(lab);
  }
  return labels.join('，');
}
