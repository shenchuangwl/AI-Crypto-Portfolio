import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AppNav } from './components/AppNav';
import { ScreenerPage } from './pages/ScreenerPage';
import { ReviewPage } from './pages/ReviewPage';
import { MarketPage } from './pages/MarketPage';
import { MarketsPage } from './pages/MarketsPage';
import { QuotesPage } from './pages/QuotesPage';
import { TerminalWorkspace } from './features/workspace/TerminalWorkspace';
import './app.css';

const qc = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, staleTime: 15_000 } },
});

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <div className="app-shell">
          <AppNav />
          <Routes>
            <Route path="/" element={<Navigate to="/terminal" replace />} />
            <Route path="/quotes" element={<QuotesPage />} />
            <Route path="/terminal" element={<TerminalWorkspace />} />
            {/* X v1.3.0 复刻 main v1.4.0，独立复盘与后续 overrides 演进；
                key 让切榜卸载旧快照，不能跨板复用组件留下上一榜一帧。 */}
            <Route path="/screener" element={<ScreenerPage key="main" board="main" />} />
            <Route path="/screener-x" element={<ScreenerPage key="x" board="x" />} />
            {/* 选币榜Y：与 /screener 同一个组件，只换 board —— 参数版本 param-v2.0.0-screener-y。
                路由、取数前缀、UI 仓库、数据目录、复盘账本全线独立，互不覆盖。 */}
            <Route path="/screener-y" element={<ScreenerPage key="y" board="y" />} />
            <Route path="/review" element={<ReviewPage />} />
            <Route path="/markets" element={<MarketsPage />} />
            <Route path="/market/:symbol" element={<MarketPage />} />
            <Route path="*" element={<Navigate to="/terminal" replace />} />
          </Routes>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
