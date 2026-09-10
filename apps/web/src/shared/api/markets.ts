export interface UniverseSymbol {
  symbol: string;
  pair?: string;
  base_asset?: string;
  quote_asset?: string;
  contract_type?: string;
  status?: string;
  underlying_type?: string;
  underlying_sub_type?: string | string[];
  spot_tags?: string[];
  categories?: string[];
  category?: string;
  market_kind?: string;
  price_precision?: number;
  quantity_precision?: number;
  onboard_date?: number;
}

export interface UniverseResponse {
  source?: string;
  version?: string;
  count: number;
  total?: number;
  stats?: Record<string, unknown>;
  symbols: UniverseSymbol[];
}

const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') || '/api/v1';

let universeCache: Promise<UniverseResponse> | null = null;

async function requestUniverse(opts?: { q?: string; kind?: string }): Promise<UniverseResponse> {
  const qs = new URLSearchParams();
  if (opts?.q) qs.set('q', opts.q);
  if (opts?.kind) qs.set('kind', opts.kind);
  const url = `${API_BASE}/markets/universe${qs.toString() ? `?${qs}` : ''}`;
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`);
  return res.json() as Promise<UniverseResponse>;
}

export async function fetchUniverse(opts?: {
  q?: string;
  kind?: string;
}): Promise<UniverseResponse> {
  if (opts?.q || opts?.kind) return requestUniverse(opts);
  if (!universeCache) {
    universeCache = requestUniverse().catch((err) => {
      universeCache = null;
      throw err;
    });
  }
  return universeCache;
}

export async function fetchSymbolMeta(symbol: string): Promise<UniverseSymbol | undefined> {
  const uni = await fetchUniverse();
  const key = symbol.toUpperCase();
  return (uni.symbols || []).find((s) => s.symbol.toUpperCase() === key);
}
