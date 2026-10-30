import { useState } from "react";
import { api, errorText, newKey } from "../api";
import { SIDE_LABEL, SOURCE_LABEL, ago, dateTime, money, num, orderStatus, signedMoney } from "../format";
import type { Dashboard as DashboardData, Mode, OrderRow, Position } from "../types";
import { connectionBadge } from "./StatusBadges";

interface Props {
  data: DashboardData | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
  children?: React.ReactNode;
}

export function Dashboard({ data, error, loading, reload, children }: Props) {
  const [actionMsg, setActionMsg] = useState<{ tone: string; text: string } | null>(null);

  if (loading && !data) return <div className="card muted">Lade Übersicht…</div>;
  if (!data) return <div className="notice bad"><strong>Übersicht nicht verfügbar</strong>{error}</div>;

  const cur = data.account?.currency ?? null;
  const conn = data.connection;
  const badge = connectionBadge(conn.connection);
  const mode = data.mode;

  async function act(fn: () => Promise<unknown>, ok: string) {
    setActionMsg(null);
    try {
      await fn();
      setActionMsg({ tone: "ok", text: ok });
    } catch (e) {
      setActionMsg({ tone: "bad", text: errorText(e) });
    } finally {
      await reload();
    }
  }

  return (
    <>
      {conn.connection !== "ok" && conn.last_error && (
        <div className="notice bad">
          <strong>
            {mode}: Brokerabruf fehlgeschlagen ({conn.last_error.kind}) – es werden keine Kontodaten angezeigt.
          </strong>
          {conn.last_error.message}
          {conn.last_error.hint && <div className="small">Nächster Schritt: {conn.last_error.hint}</div>}
        </div>
      )}
      {conn.connection === "ok" && !conn.state_clear && (
        <div className="notice warn">
          <strong>Zustand nicht vollständig abgeglichen – neue Käufe gesperrt</strong>
          {conn.issues.join(" · ")}
        </div>
      )}

      <div className="grid cols-4">
        <div className="card">
          <div className="stat-label">Verbindung ({mode})</div>
          <div className={`stat-value tone-${badge.tone}`} style={{ fontSize: 18 }}>{badge.label}</div>
          <div className="stat-sub">
            Letzter erfolgreicher Abruf: {dateTime(conn.last_ok_at)} ({ago(conn.last_ok_at)})
          </div>
        </div>
        <div className="card">
          <div className="stat-label">Cash</div>
          <div className="stat-value">{data.account ? money(data.account.cash, cur) : "—"}</div>
          <div className="stat-sub">Kontostatus: {data.account?.status ?? "—"}</div>
        </div>
        <div className="card">
          <div className="stat-label">Kontowert (gesamt)</div>
          <div className="stat-value">{data.account ? money(data.account.equity, cur) : "—"}</div>
          <div className="stat-sub">
            Buying Power {data.account ? money(data.account.buying_power, cur) : "—"} (enthält ggf. Kredit)
          </div>
        </div>
        <div className="card">
          <div className="stat-label">Heute (inkl. unrealisiert)</div>
          {data.daily.data_ok ? (
            <>
              <div className={`stat-value ${(data.daily.pnl ?? 0) < 0 ? "tone-bad" : "tone-ok"}`}>
                {signedMoney(data.daily.pnl, cur)}
              </div>
              <div className="stat-sub">
                Grenze −{money(data.daily.limit, cur)} ·{" "}
                {data.daily.locked ? <span className="tone-bad">Kaufsperre aktiv</span> : "keine Sperre"} ·
                Orders heute {data.orders_today}/{data.limits.max_orders_per_day}
              </div>
            </>
          ) : (
            <>
              <div className="stat-value muted">—</div>
              <div className="stat-sub tone-warn">{data.daily.reason}</div>
            </>
          )}
        </div>
      </div>

      {children}

      {actionMsg && <div className={`notice ${actionMsg.tone}`}>{actionMsg.text}</div>}

      <PositionsTable
        mode={mode}
        positions={data.positions}
        currency={cur}
        onClose={(p) => {
          const q = num(p.qty_available, 9);
          if (!window.confirm(`${mode}: Position ${p.symbol} schliessen?\nEs wird eine Market-Verkaufsorder über ${q} Stück (verfügbarer Bestand) gesendet.`)) return;
          void act(
            () => api.post(`/positions/${encodeURIComponent(p.symbol)}/close`, { expected_mode: mode, idempotency_key: newKey(), confirm: true }),
            `Verkaufsorder für ${p.symbol} übermittelt. Angenommen bedeutet noch nicht ausgeführt.`,
          );
        }}
      />
      <OrdersTable
        mode={mode}
        orders={data.orders}
        currency={cur}
        onCancel={(o) => {
          if (!o.broker_order_id || !window.confirm(`${mode}: Order ${o.symbol} (${SIDE_LABEL[o.side]}) stornieren?`)) return;
          void act(() => api.post(`/orders/${o.broker_order_id}/cancel`, { expected_mode: mode }), "Stornierung angefordert.");
        }}
        onCancelAll={() => {
          if (!window.confirm(`${mode}: ALLE offenen Orders stornieren? Positionen bleiben unverändert.`)) return;
          void act(() => api.post("/orders/cancel-open", { expected_mode: mode }), "Stornierung aller offenen Orders angefordert.");
        }}
      />
      <div className="small muted">Stand Brokerdaten: {dateTime(data.fetched_at)}</div>
    </>
  );
}

function PositionsTable({ mode, positions, currency, onClose }: {
  mode: Mode; positions: Position[]; currency: string | null; onClose: (p: Position) => void;
}) {
  return (
    <div className="card">
      <div className="card-head">
        <h2>Offene Positionen</h2>
        <span className="small muted">Nur Long-Positionen · Quelle: {mode === "DEMO" ? "DEMO-Simulation" : "Alpaca Paper"}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="num">Menge</th>
              <th className="num">Verfügbar</th>
              <th className="num">Ø Einstand</th>
              <th className="num">Kurs</th>
              <th className="num">Marktwert</th>
              <th className="num">Unrealisiert</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 && (
              <tr><td colSpan={8} className="empty">Keine offenen Positionen</td></tr>
            )}
            {positions.map((p) => (
              <tr key={p.symbol}>
                <td><strong>{p.symbol}</strong></td>
                <td className="num">{num(p.qty, 9)}</td>
                <td className="num">{num(p.qty_available, 9)}</td>
                <td className="num">{money(p.avg_entry_price, currency)}</td>
                <td className="num">{money(p.current_price, currency)}</td>
                <td className="num">{money(p.market_value, currency)}</td>
                <td className={`num ${p.unrealized_pl < 0 ? "tone-bad" : p.unrealized_pl > 0 ? "tone-ok" : ""}`}>
                  {signedMoney(p.unrealized_pl, currency)}
                </td>
                <td className="num">
                  <button className="btn small danger" disabled={p.qty_available <= 0} onClick={() => onClose(p)}
                          title="Sendet eine Market-Verkaufsorder über den verfügbaren Bestand (separate, ausdrückliche Aktion)">
                    Schliessen…
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function OrdersTable({ mode, orders, currency, onCancel, onCancelAll }: {
  mode: Mode; orders: OrderRow[]; currency: string | null; onCancel: (o: OrderRow) => void; onCancelAll: () => void;
}) {
  const anyOpen = orders.some((o) => o.cancelable);
  return (
    <div className="card">
      <div className="card-head">
        <h2>Aufträge</h2>
        <div className="row">
          <span className="small muted">Angenommen ≠ ausgeführt: massgeblich ist die ausgeführte Menge.</span>
          <button className="btn small danger" disabled={!anyOpen} onClick={onCancelAll}>
            Alle offenen stornieren…
          </button>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Zeit</th>
              <th>Symbol</th>
              <th>Seite</th>
              <th className="num">Auftrag</th>
              <th className="num">Ausgeführt</th>
              <th className="num">Ø Preis</th>
              <th>Status</th>
              <th>Quelle</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 && (
              <tr><td colSpan={9} className="empty">Keine Aufträge in den letzten Tagen ({mode})</td></tr>
            )}
            {orders.map((o) => {
              const st = orderStatus(o.status);
              return (
                <tr key={o.client_order_id || o.broker_order_id || Math.random()}>
                  <td className="small">{dateTime(o.created_at)}</td>
                  <td><strong>{o.symbol}</strong></td>
                  <td className={o.side === "buy" ? "tone-ok" : "tone-warn"}>{SIDE_LABEL[o.side] ?? o.side}</td>
                  <td className="num">
                    {o.qty !== null ? `${num(o.qty, 9)} Stk.` : o.notional !== null ? money(o.notional, currency) : "—"}
                    <div className="small muted">{o.type} · {o.time_in_force}</div>
                  </td>
                  <td className="num">{num(o.filled_qty, 9)}</td>
                  <td className="num">{money(o.filled_avg_price, currency)}</td>
                  <td>
                    <span className={`tone-${st.tone}`}>{st.label}</span>
                    {o.message && <div className="small muted" style={{ maxWidth: 280 }}>{o.message}</div>}
                  </td>
                  <td className="small">{SOURCE_LABEL[o.source] ?? o.source}</td>
                  <td className="num">
                    {o.cancelable && (
                      <button className="btn small" onClick={() => onCancel(o)}>Stornieren…</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
