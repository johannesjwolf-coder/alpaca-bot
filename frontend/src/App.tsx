import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { EmergencyButton } from "./components/EmergencyButton";
import { kindLabel } from "./components/SymbolPicker";
import { ConfirmProvider, Icon, Logo, ModeChip, Notice, Pill } from "./components/ui";
import { usePoll } from "./hooks";
import { BacktestPage } from "./pages/BacktestPage";
import { BotPage } from "./pages/BotPage";
import { DiagnosticsPage } from "./pages/DiagnosticsPage";
import { Overview } from "./pages/Overview";
import { SettingsPage } from "./pages/SettingsPage";
import { TradePage } from "./pages/TradePage";
import type { Dashboard, Status } from "./types";

const PAGES = [
  { id: "overview", label: "Übersicht", icon: "overview", title: "Übersicht" },
  { id: "trade", label: "Handel", icon: "trade", title: "Handel" },
  { id: "bot", label: "Bot", icon: "bot", title: "Automatik" },
  { id: "backtest", label: "Backtest", icon: "backtest", title: "Backtest" },
  { id: "settings", label: "Einstellungen", icon: "settings", title: "Einstellungen" },
  { id: "diagnostics", label: "Diagnose", icon: "diagnostics", title: "Diagnose" },
] as const;
type Page = (typeof PAGES)[number]["id"];

const pageFromHash = (): Page => (PAGES.find((p) => `#${p.id}` === window.location.hash)?.id ?? "overview") as Page;

function readSymbol(): string {
  try {
    return localStorage.getItem("symbol") || "BTC/USD";
  } catch {
    return "BTC/USD";
  }
}

const CONN: Record<string, [string, string]> = {
  ok: ["Verbunden", "ok"],
  disconnected: ["Getrennt", "warn"],
  error: ["Fehler", "bad"],
  unknown: ["Verbindung …", ""],
};

function Toolbar({ status, error, page, symbol, onMenu, refresh }: {
  status: Status | null; error: string | null; page: Page; symbol: string; onMenu: () => void; refresh: () => void;
}) {
  const meta = PAGES.find((p) => p.id === page)!;
  const conn = status ? CONN[status.connection.connection] ?? CONN.unknown : null;
  const showSymbol = page === "overview" || page === "trade";
  return (
    <header className="toolbar">
      <button className="btn icon ghost menu-btn" onClick={onMenu} aria-label="Navigation öffnen"><Icon name="menu" /></button>
      <div className="toolbar-title">
        <h1>{meta.title}</h1>
        {showSymbol && <span className="tag gold" title="Ausgewähltes Symbol">{symbol} · {kindLabel(symbol)}</span>}
      </div>
      <div className="toolbar-status" aria-label="Status">
        {status ? (
          <>
            <ModeChip mode={status.mode} />
            <Pill tone={conn![1]} title={status.connection.last_error?.message}>{conn![0]}</Pill>
            <Pill tone={status.bot.running ? "ok" : ""} running={status.bot.running} title={status.bot.pause_meaning}>
              Bot {status.bot.running ? "läuft" : "pausiert"}
            </Pill>
            {status.mode === "PAPER" && <Pill tone={status.orders_enabled ? "warn" : ""}>Orders {status.orders_enabled ? "freigegeben" : "gesperrt"}</Pill>}
            {status.market && !status.market.simulated && <Pill>US-Börse {status.market.is_open ? "offen" : "geschlossen"}</Pill>}
            <EmergencyButton status={status} onDone={refresh} />
          </>
        ) : error ? <Pill tone="bad">Backend nicht erreichbar</Pill> : <div className="skeleton" style={{ height: 26, width: 260 }} />}
      </div>
    </header>
  );
}

function Sidebar({ page, onNavigate, status }: { page: Page; onNavigate: (p: Page) => void; status: Status | null }) {
  return (
    <aside className="sidebar" aria-label="Hauptnavigation">
      <div className="brand"><Logo /><div>Alpaca Paper Bot<small>Paper Trading</small></div></div>
      <nav className="nav">
        {PAGES.map((p) => (
          <button key={p.id} className="nav-item" aria-current={page === p.id ? "page" : undefined} onClick={() => onNavigate(p.id)}>
            <Icon name={p.icon} /> {p.label}
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        {status && <ModeChip mode={status.mode} />}
        <span>Nur Paper Trading · lokal gebunden</span>
        <span>Keine Anlageberatung</span>
      </div>
    </aside>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [drawer, setDrawer] = useState(false);
  const [symbol, setSymbolState] = useState(readSymbol);
  const status = usePoll(() => api.get<Status>("/status"), 4000);
  const mode = status.data?.mode;
  const dash = usePoll(() => api.get<Dashboard>("/dashboard"), 5000, [mode]);

  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((p: string) => {
    window.location.hash = p;
    setPage(p as Page);
    setDrawer(false);
    window.scrollTo({ top: 0 });
  }, []);
  const setSymbol = useCallback((s: string) => {
    setSymbolState(s);
    try {
      localStorage.setItem("symbol", s);
    } catch {
      /* nur Komfort */
    }
  }, []);
  const refresh = useCallback(() => {
    void status.reload();
    void dash.reload();
  }, [status, dash]);

  const s = status.data;
  return (
    <ConfirmProvider>
      <div className={`shell ${drawer ? "drawer-open" : ""}`}>
        <Sidebar page={page} onNavigate={navigate} status={s} />
        <div className="drawer-backdrop" onClick={() => setDrawer(false)} aria-hidden="true" />
        <div className="main">
          <Toolbar status={s} error={status.error} page={page} symbol={symbol} onMenu={() => setDrawer(true)} refresh={refresh} />
          {s?.mode === "DEMO" && (
            <div className="demo-strip" role="status"><Icon name="info" size={16} /> DEMO-Modus: simulierte, reproduzierbare Beispieldaten – keine Verbindung zu Alpaca, keine echten Kontodaten.</div>
          )}
          <main className="content">
            {!s && status.error && (
              <Notice tone="error" title="Backend nicht erreichbar" actions={<button className="btn sm" onClick={refresh}><Icon name="refresh" size={15} /> Erneut versuchen</button>}>
                {status.error} – läuft das Backend? Start im Ordner <code>backend</code>: <code>uv run python -m app</code>
              </Notice>
            )}
            {!s && !status.error && <div className="kpis">{[0, 1, 2, 3].map((i) => <div key={i} className="card kpi"><div className="skeleton" style={{ height: 80 }} /></div>)}</div>}
            {s && page === "overview" && <Overview status={s} dash={dash} symbol={symbol} onSymbol={setSymbol} refresh={refresh} onNavigate={navigate} />}
            {s && page === "trade" && <TradePage status={s} dash={dash} symbol={symbol} onSymbol={setSymbol} refresh={refresh} onNavigate={navigate} />}
            {s && page === "bot" && <BotPage status={s} onChange={refresh} />}
            {s && page === "backtest" && <BacktestPage status={s} />}
            {s && page === "settings" && <SettingsPage status={s} onChange={refresh} />}
            {s && page === "diagnostics" && <DiagnosticsPage mode={s.mode} />}
            <p className="footer">Nur Paper Trading · lokal gebunden · keine Anlageberatung · Teststrategien ohne Gewinnanspruch</p>
          </main>
        </div>
      </div>
    </ConfirmProvider>
  );
}
