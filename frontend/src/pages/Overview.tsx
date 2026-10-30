import { useMemo, useState } from "react";
import { api } from "../api";
import { BotCard, useBotInfo } from "../components/BotCard";
import { ChartPanel, ordersToMarkers } from "../components/chart/ChartPanel";
import { OrderTicket } from "../components/OrderTicket";
import { useSymbolSuggestions } from "../components/SymbolPicker";
import { BotTradesTable, OrdersTable, PositionsTable } from "../components/tables";
import { Icon, Kpi, Notice, SkeletonRows, Tabs } from "../components/ui";
import { ago, amount2, dateTime, money, num, pct, signedMoney, tone } from "../format";
import type { Dashboard, Status } from "../types";

type BottomTab = "positions" | "open" | "history" | "trades";

export function KpiRow({ data, loading }: { data: Dashboard | null; loading: boolean }) {
  const acct = data?.account ?? null;
  const cur = acct?.currency ?? null;
  const daily = data?.daily;
  const pnl = daily?.data_ok ? daily.pnl ?? null : null;
  const pnlPct = pnl !== null && daily?.baseline ? (pnl / daily.baseline) * 100 : null;
  const positions = data?.positions ?? [];
  const mv = positions.reduce((a, p) => a + p.market_value, 0);
  const upl = positions.reduce((a, p) => a + p.unrealized_pl, 0);
  const wait = loading && !data;
  return (
    <div className="kpis">
      <Kpi loading={wait} label="Kontowert" value={acct ? amount2(acct.equity) : "—"} unit={cur ?? undefined}
           sub={acct && pnl !== null ? <span className={`delta ${tone(pnl)}`}>{signedMoney(pnl, cur)} ({pct(pnlPct)}) heute</span> : <span>Gesamtwert inkl. Positionen</span>} />
      <Kpi loading={wait} label="Verfügbares Guthaben" value={acct ? amount2(acct.cash) : "—"} unit={cur ?? undefined}
           sub={acct ? <span>Buying Power {money(acct.buying_power, cur)} (inkl. möglichem Kredit)</span> : <span>Cash des Kontos</span>} />
      <Kpi loading={wait} label="Tagesergebnis" value={pnl !== null ? <span className={tone(pnl)}>{pnl > 0 ? "+" : pnl < 0 ? "−" : ""}{amount2(Math.abs(pnl))}</span> : "—"} unit={pnl !== null ? cur ?? undefined : undefined}
           sub={daily ? (daily.data_ok ? (
             <span>Grenze −{money(daily.limit, cur)} · {daily.locked ? <strong className="neg">Kaufsperre aktiv</strong> : "keine Sperre"}</span>
           ) : <span>{daily.reason}</span>) : null} />
      <Kpi loading={wait} label="Offene Positionen" value={acct ? String(positions.length) : "—"}
           sub={acct ? <span>Marktwert {money(mv, cur)} · unrealisiert <span className={tone(upl)}>{signedMoney(upl, cur)}</span></span> : <span>Keine aktuellen Brokerdaten</span>} />
    </div>
  );
}

export function ConnectionNotice({ data, onRetry, onNavigate }: { data: Dashboard | null; onRetry: () => void; onNavigate: (p: string) => void }) {
  const conn = data?.connection;
  if (!conn) return null;
  if (conn.connection !== "ok" && conn.last_error)
    return (
      <Notice tone="error" title={`${conn.mode}: Brokerabruf fehlgeschlagen (${conn.last_error.kind}) – es werden keine Kontodaten angezeigt`}
              actions={<>
                <button className="btn sm" onClick={onRetry}><Icon name="refresh" size={15} /> Erneut versuchen</button>
                <button className="btn sm ghost" onClick={() => onNavigate("diagnostics")}>Verbindung testen</button>
              </>}>
        {conn.last_error.message}
        {conn.last_error.hint && <div className="small">Nächster Schritt: {conn.last_error.hint}</div>}
      </Notice>
    );
  if (conn.connection === "ok" && !conn.state_clear)
    return <Notice tone="warn" title="Zustand wird abgeglichen – neue Käufe sind gesperrt">{conn.issues.join(" · ")}</Notice>;
  return null;
}

export function BottomTables({ data, onDone }: { data: Dashboard | null; onDone: () => void }) {
  const [tab, setTab] = useState<BottomTab>("positions");
  const bot = useBotInfo(data?.mode ?? "", 10_000);
  const cur = data?.account?.currency ?? null;
  const open = data?.orders.filter((o) => o.cancelable).length ?? 0;
  return (
    <section className="card" aria-label="Positionen und Orders">
      <Tabs<BottomTab> label="Tabellen" value={tab} onChange={setTab} tabs={[
        { id: "positions", label: "Positionen", count: data?.positions.length },
        { id: "open", label: "Offene Orders", count: open },
        { id: "history", label: "Orderverlauf", count: data?.orders.length },
        { id: "trades", label: "Bot-Trades", count: bot.data?.trades.length },
      ]} />
      {!data ? <SkeletonRows /> : (
        <>
          {tab === "positions" && <PositionsTable mode={data.mode} positions={data.positions} currency={cur} onDone={onDone} />}
          {tab === "open" && <OrdersTable mode={data.mode} orders={data.orders} currency={cur} onDone={onDone} variant="open" />}
          {tab === "history" && <OrdersTable mode={data.mode} orders={data.orders} currency={cur} onDone={onDone} variant="history" />}
          {tab === "trades" && <BotTradesTable trades={bot.data?.trades ?? []} />}
        </>
      )}
      <div className="card-foot">
        Brokerstand {dateTime(data?.fetched_at)} ({ago(data?.fetched_at)}) · Orders heute {num(data?.orders_today ?? 0, 0)}/{data?.limits.max_orders_per_day ?? "—"}
      </div>
    </section>
  );
}

export function Overview({ status, dash, symbol, onSymbol, refresh, onNavigate }: {
  status: Status; dash: { data: Dashboard | null; loading: boolean; reload: () => Promise<void> };
  symbol: string; onSymbol: (s: string) => void; refresh: () => void; onNavigate: (p: string) => void;
}) {
  const suggestions = useSymbolSuggestions(status.mode);
  const data = dash.data && dash.data.mode === status.mode ? dash.data : null;
  const markers = useMemo(
    () => ordersToMarkers(data?.orders ?? [], symbol, data?.account?.currency ?? null),
    [data?.orders, symbol, data?.account?.currency],
  );
  return (
    <>
      <ConnectionNotice data={data} onRetry={() => void api.post("/sync").then(refresh, refresh)} onNavigate={onNavigate} />
      <KpiRow data={data} loading={dash.loading} />
      <div className="workspace">
        <ChartPanel mode={status.mode} symbol={symbol} onSymbol={onSymbol} suggestions={suggestions.data?.items ?? []} markers={markers} />
        <div className="side-stack">
          <OrderTicket status={status} symbol={symbol} onSymbol={onSymbol} onDone={refresh} onNavigate={onNavigate} />
          <BotCard status={status} onChange={refresh} onNavigate={onNavigate} />
        </div>
      </div>
      <BottomTables data={data} onDone={refresh} />
    </>
  );
}
