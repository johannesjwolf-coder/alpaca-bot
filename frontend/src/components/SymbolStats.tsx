import { useState } from "react";
import { api, errorText } from "../api";
import { dateTime, num, signedMoney, tone } from "../format";
import { usePoll } from "../hooks";
import type { BotStats, Mode } from "../types";
import { Card, EmptyState, Notice, Seg, Tag, useConfirm } from "./ui";

type Period = "7" | "30" | "0";

/** Kennzahlen je Symbol mit Auto-Pause-Status und manuellem Fortsetzen. */
export function SymbolStatsCard({ mode }: { mode: Mode }) {
  const [period, setPeriod] = useState<Period>("30");
  const stats = usePoll(() => api.get<BotStats>(`/bot/stats?days=${period}`), 30_000, [mode, period]);
  const confirm = useConfirm();
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);
  const rows = stats.data?.symbols ?? [];
  const rule = stats.data?.pause_rule;
  const anyTrades = rows.some((r) => r.trades > 0 || r.paused_until);

  async function resume(symbol: string) {
    const ok = await confirm({
      title: `${symbol} fortsetzen?`,
      confirmLabel: "Fortsetzen",
      body: <p>Der Bot darf mit {symbol} wieder einsteigen. Die Auto-Pause bewertet danach die nächsten {rule?.trades ?? 10} Trades neu.</p>,
    });
    if (!ok) return;
    setMsg(null);
    try {
      await api.post("/bot/symbols/resume", { expected_mode: mode, symbol });
      setMsg({ tone: "ok", text: `${symbol} wieder freigegeben.` });
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    } finally {
      void stats.reload();
    }
  }

  return (
    <Card
      title="Statistik je Symbol"
      bodyClass={null}
      actions={
        <Seg label="Zeitraum" value={period} onChange={setPeriod}
             options={[{ id: "7", label: "7 Tage" }, { id: "30", label: "30 Tage" }, { id: "0", label: "Gesamt" }]} />
      }
    >
      {msg && <div style={{ padding: "12px 20px 0" }}><Notice tone={msg.tone}>{msg.text}</Notice></div>}
      {!anyTrades ? (
        <EmptyState title="Noch keine abgeschlossenen Bot-Trades im Zeitraum">
          Kennzahlen je Symbol erscheinen, sobald der Bot Trades abgeschlossen hat.
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Status</th>
                <th className="r">Trades</th>
                <th className="r">Trefferquote</th>
                <th className="r">Netto</th>
                <th className="r">Ø je Trade</th>
                <th className="r">Profit-Faktor</th>
                <th className="r">Ø Haltedauer</th>
                <th>Ausstiege</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.symbol}>
                  <td className="sym">{r.symbol}</td>
                  <td className="wrap">
                    {r.paused_until ? (
                      <>
                        <Tag tone="warn">pausiert bis {dateTime(r.paused_until)}</Tag>
                        <div className="muted small" style={{ marginTop: 4 }}>{r.pause_reason}</div>
                        <button className="btn sm" style={{ marginTop: 6 }} onClick={() => void resume(r.symbol)}>Fortsetzen…</button>
                      </>
                    ) : r.active ? <Tag tone="ok">aktiv</Tag> : <Tag>nicht in der Liste</Tag>}
                  </td>
                  <td className="r">{r.trades}<div className="muted small">{r.wins} Gew. · {r.losses} Verl.</div></td>
                  <td className="r">{r.win_rate_pct !== null ? `${num(r.win_rate_pct, 1)} %` : "—"}</td>
                  <td className={`r ${tone(r.net)}`}><strong>{signedMoney(r.net, "USD")}</strong></td>
                  <td className={`r ${tone(r.avg_net)}`}>{signedMoney(r.avg_net, "USD")}</td>
                  <td className="r">{r.profit_factor !== null ? num(r.profit_factor, 2) : "—"}</td>
                  <td className="r">{r.avg_hold_min !== null ? `${num(r.avg_hold_min, 1)} Min.` : "—"}</td>
                  <td className="wrap small">{Object.entries(r.exit_reasons).map(([k, v]) => `${k}: ${v}`).join(" · ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="card-foot">
        {rule?.enabled
          ? `Auto-Pause: Sind die letzten ${rule.trades} Trades eines Symbols zusammen im Minus, pausiert es ${num(rule.hours, 0)} Std. (nur neue Einstiege).`
          : "Auto-Pause ist ausgeschaltet."}{" "}
        Profit-Faktor = Summe der Gewinne ÷ Summe der Verluste (über 1 = netto positiv). {stats.data?.note}
      </div>
    </Card>
  );
}
