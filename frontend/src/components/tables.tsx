import { useState } from "react";
import { api, errorText, newKey } from "../api";
import { localTz, money, num, orderStatus, pct, price, shortDT, SIDE_LABEL, signedMoney, SOURCE_LABEL, tone } from "../format";
import type { Mode, OrderRow, Position, Trade } from "../types";
import { EmptyState, Notice, Tag, useConfirm } from "./ui";

const STATUS_TONE: Record<string, string> = { ok: "ok", info: "info", warn: "warn", bad: "bad", muted: "" };

function useAction(onDone: () => void) {
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setMsg(null);
    try {
      await fn();
      setMsg({ tone: "ok", text: ok });
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    } finally {
      onDone();
    }
  };
  return { msg, run };
}

export function PositionsTable({ mode, positions, currency, onDone }: {
  mode: Mode; positions: Position[]; currency: string | null; onDone: () => void;
}) {
  const confirm = useConfirm();
  const { msg, run } = useAction(onDone);
  if (!positions.length) return <EmptyState title="Keine offenen Positionen">Gekaufte Bestände erscheinen hier mit Einstand und Ergebnis.</EmptyState>;
  return (
    <>
      {msg && <div style={{ padding: "12px 20px 0" }}><Notice tone={msg.tone}>{msg.text}</Notice></div>}
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="r">Menge</th>
              <th className="r">Verfügbar</th>
              <th className="r">Ø Einstand</th>
              <th className="r">Kurs</th>
              <th className="r">Marktwert</th>
              <th className="r">Unrealisiert</th>
              <th className="r">Aktion</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => {
              const plPct = p.cost_basis ? (p.unrealized_pl / p.cost_basis) * 100 : null;
              return (
                <tr key={p.symbol}>
                  <td><span className="sym">{p.symbol}</span> <span className="muted small">{p.symbol.includes("/") ? "Krypto" : "Aktie/ETF"}</span></td>
                  <td className="r">{num(p.qty, 9)}</td>
                  <td className="r">{num(p.qty_available, 9)}</td>
                  <td className="r">{price(p.avg_entry_price)}</td>
                  <td className="r">{price(p.current_price)}</td>
                  <td className="r">{money(p.market_value, currency)}</td>
                  <td className={`r ${tone(p.unrealized_pl)}`}>
                    {signedMoney(p.unrealized_pl, currency)} <span className="small">({pct(plPct)})</span>
                  </td>
                  <td className="r">
                    <button className="btn sm danger" disabled={p.qty_available <= 0}
                            title="Market-Verkauf des verfügbaren Bestands (separate, ausdrückliche Aktion)"
                            onClick={async () => {
                              const ok = await confirm({
                                title: `Position ${p.symbol} schliessen?`,
                                tone: "sell",
                                confirmLabel: `${num(p.qty_available, 9)} ${p.symbol} verkaufen`,
                                body: (
                                  <>
                                    <p>Es wird eine <strong>Market-Verkaufsorder</strong> über den verfügbaren Bestand gesendet.</p>
                                    <dl className="kv"><dt>Modus</dt><dd>{mode}</dd><dt>Symbol</dt><dd>{p.symbol}</dd><dt>Menge</dt><dd>{num(p.qty_available, 9)}</dd><dt>Marktwert ca.</dt><dd>{money(p.market_value, currency)}</dd></dl>
                                  </>
                                ),
                              });
                              if (!ok) return;
                              await run(
                                () => api.post(`/positions/${encodeURIComponent(p.symbol)}/close`, { expected_mode: mode, idempotency_key: newKey(), confirm: true }),
                                `Verkaufsorder für ${p.symbol} übermittelt. Angenommen bedeutet noch nicht ausgeführt.`,
                              );
                            }}>
                      Schliessen…
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function OrdersTable({ mode, orders, currency, onDone, variant }: {
  mode: Mode; orders: OrderRow[]; currency: string | null; onDone: () => void; variant: "open" | "history";
}) {
  const confirm = useConfirm();
  const { msg, run } = useAction(onDone);
  const rows = variant === "open" ? orders.filter((o) => o.cancelable) : orders;
  if (!rows.length)
    return (
      <EmptyState title={variant === "open" ? "Keine offenen Orders" : "Noch keine Orders"}>
        {variant === "open" ? "Gesendete, noch nicht abgeschlossene Aufträge erscheinen hier." : `Aufträge der letzten Tage (${mode}).`}
      </EmptyState>
    );
  return (
    <>
      {msg && <div style={{ padding: "12px 20px 0" }}><Notice tone={msg.tone}>{msg.text}</Notice></div>}
      {variant === "open" && (
        <div className="row" style={{ padding: "12px 20px 0" }}>
          <span className="muted small">Angenommen heisst noch nicht ausgeführt – massgeblich ist die ausgeführte Menge.</span>
          <span className="spacer" />
          <button className="btn sm danger" onClick={async () => {
            const ok = await confirm({
              title: "Alle offenen Orders stornieren?", tone: "sell", confirmLabel: `${rows.length} Order(s) stornieren`,
              body: <p>Modus <strong>{mode}</strong>. Positionen bleiben unverändert.</p>,
            });
            if (ok) await run(() => api.post("/orders/cancel-open", { expected_mode: mode }), "Stornierung aller offenen Orders angefordert.");
          }}>Alle stornieren…</button>
        </div>
      )}
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Zeit ({localTz})</th>
              <th>Symbol</th>
              <th>Seite</th>
              <th className="r">Auftrag</th>
              <th className="r">Ausgeführt</th>
              <th className="r">Ø Preis</th>
              <th>Status</th>
              <th>Quelle</th>
              {variant === "open" && <th className="r">Aktion</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((o) => {
              const st = orderStatus(o.status);
              return (
                <tr key={o.client_order_id || o.broker_order_id || o.created_at || ""}>
                  <td className="muted">{shortDT(o.created_at)}</td>
                  <td className="sym">{o.symbol}</td>
                  <td className={o.side === "buy" ? "side-buy" : "side-sell"}>{SIDE_LABEL[o.side] ?? o.side}</td>
                  <td className="r">{o.qty !== null ? `${num(o.qty, 9)} Stk.` : o.notional !== null ? money(o.notional, currency) : "—"}<div className="muted small">{o.type} · {o.time_in_force}</div></td>
                  <td className="r">{num(o.filled_qty, 9)}</td>
                  <td className="r">{price(o.filled_avg_price)}</td>
                  <td className="wrap"><Tag tone={STATUS_TONE[st.tone] ?? ""}>{st.label}</Tag>{o.message && <div className="muted small" style={{ marginTop: 4 }}>{o.message}</div>}</td>
                  <td className="muted">{SOURCE_LABEL[o.source] ?? o.source}</td>
                  {variant === "open" && (
                    <td className="r">
                      {o.broker_order_id && (
                        <button className="btn sm" onClick={async () => {
                          const ok = await confirm({
                            title: `Order ${o.symbol} stornieren?`, tone: "sell", confirmLabel: "Stornieren",
                            body: <dl className="kv"><dt>Modus</dt><dd>{mode}</dd><dt>Auftrag</dt><dd>{SIDE_LABEL[o.side]} {o.symbol} · {o.qty !== null ? `${num(o.qty, 9)} Stk.` : money(o.notional, currency)}</dd></dl>,
                          });
                          if (ok) await run(() => api.post(`/orders/${o.broker_order_id}/cancel`, { expected_mode: mode }), "Stornierung angefordert.");
                        }}>Stornieren…</button>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

const TRADE_STATUS: Record<Trade["status"], [string, string]> = {
  entering: ["Einstieg läuft", "warn"],
  open: ["Offen", "info"],
  exiting: ["Ausstieg läuft", "warn"],
  closed: ["Geschlossen", ""],
  canceled: ["Abgebrochen", ""],
};

export function BotTradesTable({ trades }: { trades: Trade[] }) {
  if (!trades.length) return <EmptyState title="Noch keine Bot-Trades">Einstiege und Ausstiege der Automatik erscheinen hier mit Ergebnis nach Kosten.</EmptyState>;
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>Einstieg ({localTz})</th>
            <th>Symbol</th>
            <th className="r">Menge</th>
            <th className="r">Einstiegskurs</th>
            <th className="r">Ausstiegskurs</th>
            <th>Grund</th>
            <th className="r">Brutto</th>
            <th className="r">Kosten (gesch.)</th>
            <th className="r">Netto</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => {
            const [label, tn] = TRADE_STATUS[t.status];
            return (
              <tr key={t.id}>
                <td className="muted">{shortDT(t.entry_time ?? t.created_at)}</td>
                <td className="sym">{t.symbol}</td>
                <td className="r">{num(t.entry_qty, 8)}</td>
                <td className="r">{price(t.entry_price)}</td>
                <td className="r">{price(t.exit_price)}</td>
                <td>{t.exit_reason ?? "—"}</td>
                <td className={`r ${tone(t.gross_pnl)}`}>{signedMoney(t.gross_pnl, "USD")}</td>
                <td className="r muted">{money(t.fees_est, "USD")}</td>
                <td className={`r ${tone(t.net_pnl)}`}><strong>{signedMoney(t.net_pnl, "USD")}</strong></td>
                <td className="wrap"><Tag tone={tn}>{label}</Tag>{t.note && <div className="muted small" style={{ marginTop: 4 }}>{t.note}</div>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
