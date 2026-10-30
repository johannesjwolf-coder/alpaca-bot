import { useEffect, useState } from "react";
import { api } from "./api";
import { BotPanel } from "./components/BotPanel";
import { Dashboard } from "./components/Dashboard";
import { Diagnostics } from "./components/Diagnostics";
import { OrderForm } from "./components/OrderForm";
import { PriceChart } from "./components/PriceChart";
import { SettingsPanel } from "./components/SettingsPanel";
import { StatusBadges } from "./components/StatusBadges";
import { usePoll } from "./hooks";
import type { Dashboard as DashboardData, Status } from "./types";

const TABS = [
  ["overview", "Übersicht"],
  ["trade", "Handel"],
  ["bot", "Bot"],
  ["settings", "Einstellungen"],
  ["diagnostics", "Diagnose"],
] as const;
type Tab = (typeof TABS)[number][0];

function tabFromHash(): Tab {
  const h = window.location.hash.replace("#", "");
  return (TABS.find(([k]) => k === h)?.[0] ?? "overview") as Tab;
}

const BANNER = {
  DEMO: "DEMO-MODUS · simulierte, reproduzierbare Beispieldaten · keine Verbindung zu Alpaca · keine echten Kontodaten",
  PAPER: "PAPER-MODUS · Alpaca-Paper-Konto (Spielgeld) · Marktdaten IEX · kein Echtgeld",
};

export default function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const status = usePoll(() => api.get<Status>("/status"), 4000);
  const mode = status.data?.mode;
  const dash = usePoll(() => api.get<DashboardData>("/dashboard"), 5000, [mode]);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const refresh = () => {
    void status.reload();
    void dash.reload();
  };

  return (
    <>
      <header className="topbar">
        {mode && (
          <div className={`mode-banner ${mode}`} role="status">
            {BANNER[mode]}
          </div>
        )}
        <div className="topbar-inner">
          <div className="brand">
            <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
              <rect width="32" height="32" rx="7" fill="#1a202b" />
              <path d="M6 22l6-7 5 4 9-11" stroke="#53c7a8" strokeWidth="3" fill="none" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Alpaca Paper Bot
          </div>
          <StatusBadges status={status.data} backendError={status.error} />
          <nav className="tabs" aria-label="Bereiche">
            {TABS.map(([k, label]) => (
              <button
                key={k}
                className={`tab ${tab === k ? "active" : ""}`}
                onClick={() => {
                  window.location.hash = k;
                  setTab(k);
                }}
              >
                {label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      <main>
        {!status.data && status.error && (
          <div className="notice bad">
            <strong>Backend nicht erreichbar</strong>
            {status.error} – Startet das Backend? (siehe README: <code>uv run python -m app</code>)
          </div>
        )}
        {status.data && tab === "overview" && (
          <Dashboard data={dash.data} error={dash.error} loading={dash.loading} reload={dash.reload}>
            <PriceChart key={status.data.mode} mode={status.data.mode} defaultSymbol="SPY" />
          </Dashboard>
        )}
        {status.data && tab === "trade" && <OrderForm status={status.data} onDone={refresh} />}
        {status.data && tab === "bot" && <BotPanel status={status.data} onChange={refresh} />}
        {status.data && tab === "settings" && <SettingsPanel status={status.data} onChange={refresh} />}
        {status.data && tab === "diagnostics" && <Diagnostics mode={status.data.mode} />}
        <footer className="small muted" style={{ textAlign: "center", padding: "10px 0 24px" }}>
          Nur Paper Trading · lokal gebunden · keine Anlageberatung · Beispielstrategie ohne Gewinnanspruch
        </footer>
      </main>
    </>
  );
}
