import { useEffect, useState } from "react";
import { api, errorText } from "../api";
import { useBotInfo } from "../components/BotCard";
import { BotTradesTable } from "../components/tables";
import { Card, EmptyState, Icon, Notice, Pill, Seg, Spinner, Tag, useConfirm } from "../components/ui";
import { ago, dateTime, localTz, money, num, pct, price, shortDT, signedMoney, tone } from "../format";
import type { BotParams, ScalperState, Status } from "../types";

const ACTION: Record<string, [string, string]> = {
  none: ["Keine Aktion", ""],
  skip: ["Übersprungen", "warn"],
  blocked: ["Gesperrt (Risiko)", "bad"],
  failed: ["Broker lehnte ab", "bad"],
  order: ["Order gesendet", "ok"],
};
type NumKey = Exclude<keyof BotParams, "strategy" | "symbol">;
const SCALP_FIELDS: [NumKey, string, string, string?][] = [
  ["order_value", "Einsatz je Trade", "1", "USD"],
  ["stop_loss_pct", "Stop-Loss", "0.05", "%"],
  ["take_profit_pct", "Take-Profit", "0.05", "%"],
  ["max_hold_min", "Max. Haltedauer", "1", "Min."],
  ["bar_seconds", "Mikrokerze", "1", "Sek."],
  ["short_window", "Kurzer Ø", "1", "Kerzen"],
  ["long_window", "Langer Ø", "1", "Kerzen"],
  ["poll_interval_s", "Kursabruf alle", "0.5", "Sek."],
  ["fee_pct", "Gebühr je Ausführung", "0.01", "%"],
];
const SMA_FIELDS: [NumKey, string, string, string?][] = [
  ["order_value", "Zielwert je Kauf", "1", "USD"],
  ["short_window", "Kurzer SMA", "1", "Minuten"],
  ["long_window", "Langer SMA", "1", "Minuten"],
  ["max_bar_age_s", "Max. Kerzenalter", "10", "Sek."],
];

function LiveCard({ live }: { live: ScalperState | null | undefined }) {
  const q = live?.quote;
  const t = live?.trade;
  const remaining = t?.deadline ? Math.max(0, Math.round((new Date(t.deadline).getTime() - Date.now()) / 1000)) : null;
  return (
    <Card title={`Live${live?.symbol ? `: ${live.symbol}` : ""}`} actions={q ? <Tag tone={q.fresh ? "ok" : "warn"}>{q.fresh ? "Kurs frisch" : "Kurs veraltet"}</Tag> : undefined}>
      {!live ? (
        <EmptyState icon="bot" title="Bot pausiert">Der Live-Status erscheint, solange der Scalping-Bot läuft.</EmptyState>
      ) : (
        <div className="stack">
          <dl className="kv">
            <dt>Status</dt><dd>{live.status ?? "—"}</dd>
            <dt>Geld / Brief</dt><dd>{q ? `${price(q.bid)} / ${price(q.ask)}` : "—"}</dd>
            <dt>Spread</dt><dd>{q ? pct(q.spread_pct, 3, false) : "—"}</dd>
            <dt>Kurszeit</dt><dd>{q ? `${dateTime(q.at)} (${ago(q.at)})` : "—"}</dd>
            {live.bars_needed !== undefined && live.bars_ready !== undefined && live.bars_ready < live.bars_needed && (
              <><dt>Aufwärmen</dt><dd>{live.bars_ready}/{live.bars_needed} Mikrokerzen</dd></>
            )}
            {live.last_eval && <><dt>Durchschnitte</dt><dd>kurz {price(live.last_eval.sma_short)} · lang {price(live.last_eval.sma_long)}</dd></>}
          </dl>
          {t && (
            <Notice title={`Offene Position #${t.id}: ${num(t.qty, 8)} zu ${price(t.entry_price)}`}>
              Geldkurs {price(t.bid ?? null)} · <span className={tone(t.pnl_pct)}>{pct(t.pnl_pct ?? null, 3)}</span><br />
              Stop-Loss {price(t.stop_price)} · Take-Profit {price(t.target_price)} · Restzeit{" "}
              {remaining !== null ? `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")} Min.` : "—"}
            </Notice>
          )}
        </div>
      )}
    </Card>
  );
}

export function BotPage({ status, onChange }: { status: Status; onChange: () => void }) {
  const mode = status.mode;
  const info = useBotInfo(mode);
  const confirm = useConfirm();
  const [params, setParams] = useState<BotParams | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);
  const running = status.bot.running;

  useEffect(() => {
    if (info.data && !params) setParams(info.data.params);
  }, [info.data, params]);
  useEffect(() => setParams(null), [mode]);

  async function act(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      setMsg({ tone: "ok", text: ok });
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
      onChange();
      void info.reload();
    }
  }

  const scalp = params?.strategy === "scalp";
  const cost = params ? 2 * params.fee_pct : 0;
  const summary = info.data?.summary;
  const saved = info.data?.params;

  return (
    <>
      <div className="grid-2">
        <Card title="Automatik" actions={<Pill tone={running ? "ok" : ""} running={running}>{running ? `läuft · ${status.bot.strategy === "scalp" ? "Scalping" : "SMA"}` : "pausiert"}</Pill>}>
          <div className="stack">
            <div className="row">
              {running ? (
                <button className="btn" disabled={busy} onClick={() => act(() => api.post("/bot/pause"), "Bot pausiert.")}>
                  {busy ? <Spinner /> : <Icon name="pause" size={16} />} Pausieren
                </button>
              ) : (
                <button className="btn primary" disabled={busy || (mode === "PAPER" && !status.orders_enabled) || !saved}
                        onClick={async () => {
                          if (!saved) return;
                          const ok = await confirm({
                            title: "Automatik starten?", confirmLabel: "Bot starten",
                            body: <dl className="kv"><dt>Modus</dt><dd><strong>{mode}</strong></dd><dt>Strategie</dt><dd>{saved.strategy === "scalp" ? "Scalping Krypto" : "SMA Aktien"}</dd><dt>Symbol</dt><dd>{saved.symbol}</dd><dt>Einsatz je Trade</dt><dd>{money(saved.order_value, "USD")}</dd></dl>,
                          });
                          if (ok) await act(() => api.post("/bot/start", { expected_mode: mode }), "Bot gestartet.");
                        }}>
                  {busy ? <Spinner /> : <Icon name="play" size={16} />} Bot starten
                </button>
              )}
              <span className="muted sm">{status.bot.last_status || "—"}</span>
            </div>
            <Notice title="Was „Pausieren“ bedeutet">
              Pausieren stoppt alle neuen automatischen Aufträge – beim Scalping auch Stop-Loss, Take-Profit und Haltedauer.
              Offene Positionen bleiben bestehen. Zum Aussteigen: „Schliessen…“ in der Positionstabelle oder <b>Not-Aus</b> oben rechts.
            </Notice>
            {mode === "PAPER" && !status.orders_enabled && <Notice tone="warn">Start im PAPER-Modus erst nach ausdrücklicher Paper-Orderfreigabe (Einstellungen).</Notice>}
            {summary && (
              <dl className="kv">
                <dt>Trades heute</dt><dd>{summary.trades_today} · <span className="pos">{summary.wins_today} Gewinner</span> · <span className="neg">{summary.losses_today} Verlierer</span></dd>
                <dt>Netto heute (geschätzt)</dt><dd className={tone(summary.net_today)}><strong>{signedMoney(summary.net_today, "USD")}</strong></dd>
              </dl>
            )}
            {summary?.loss_pause && <Notice tone="warn" title="Kaufpause nach Verlustserie">{summary.loss_pause.reason} – Käufe bis {dateTime(summary.loss_pause.until)} gesperrt. Verkäufe bleiben möglich.</Notice>}
            {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
          </div>
        </Card>
        <LiveCard live={running && status.bot.strategy === "scalp" ? info.data?.state.scalper ?? status.bot.scalper : null} />
      </div>

      <Card title="Strategie und Parameter" hint={running ? "Änderungen nur bei pausiertem Bot" : "Technische Paper-Testwerte"}>
        {!params ? <div className="skeleton" style={{ height: 160 }} /> : (
          <form className="stack" onSubmit={(e) => { e.preventDefault(); void act(() => api.put("/settings/bot", { ...params, expected_mode: mode }), "Parameter gespeichert."); }}>
            <Seg label="Strategie" value={params.strategy} disabled={running}
                 onChange={(v) => info.data && setParams({ ...info.data.defaults[v] })}
                 options={[{ id: "scalp", label: "Scalping Krypto" }, { id: "sma", label: "SMA Aktien (Minuten)" }]} />
            <div className="form-grid">
              <label className="field">
                <span>Symbol {scalp ? "(Krypto, z. B. BTC/USD)" : "(US-Aktie/ETF)"}</span>
                <input value={params.symbol} disabled={running} onChange={(e) => setParams({ ...params, symbol: e.target.value.toUpperCase() })} />
              </label>
              {(scalp ? SCALP_FIELDS : SMA_FIELDS).map(([k, label, step, unit]) => (
                <label className="field" key={k}>
                  <span>{label}{unit ? ` (${unit})` : ""}</span>
                  <input type="number" step={step} min={0} value={params[k]} disabled={running}
                         onChange={(e) => setParams({ ...params, [k]: Number(e.target.value) })} />
                </label>
              ))}
            </div>
            {scalp && (
              <Notice tone={params.take_profit_pct <= cost ? "warn" : ""} title="Kostenrechnung je Trade (geschätzt)">
                Gebühren für Kauf und Verkauf ≈ {num(cost, 2)} % plus Spread. Take-Profit {num(params.take_profit_pct, 2)} % → netto höchstens ≈{" "}
                {pct(params.take_profit_pct - cost)}; Stop-Loss {num(params.stop_loss_pct, 2)} % → netto ≈ {pct(-(params.stop_loss_pct + cost))}.
                {params.take_profit_pct <= cost && " Das Ziel liegt nicht über den Kosten – netto ist kein Gewinn möglich."}
              </Notice>
            )}
            <div className="row">
              <button className="btn primary" type="submit" disabled={busy || running}>Parameter speichern</button>
              <span className="field-help">
                {scalp ? "Teststrategie: Kurs alle paar Sekunden, Mikrokerzen, Einstieg bei Aufwärtskreuzung, Ausstieg per Stop-Loss, Take-Profit oder Haltedauer. Keine Gewinnzusage."
                       : info.data?.disclaimer}
              </span>
            </div>
          </form>
        )}
      </Card>

      <Card title="Bot-Trades" hint="Kosten geschätzt; massgeblich ist die Abrechnung des Brokers" bodyClass={null}>
        <BotTradesTable trades={info.data?.trades ?? []} />
      </Card>

      <Card title="Entscheidungsverlauf" hint="Einstiege, Ausstiege und gesperrte Signale mit Begründung" bodyClass={null}>
        {!info.data?.decisions.length ? <EmptyState title={`Noch keine Entscheidungen (${mode})`} /> : (
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>Zeit ({localTz})</th><th>Symbol</th><th className="r">Kurs</th><th className="r">Ø kurz</th><th className="r">Ø lang</th><th>Signal</th><th>Aktion</th><th>Begründung</th></tr></thead>
              <tbody>
                {info.data.decisions.map((d) => {
                  const [label, tn] = ACTION[d.action] ?? [d.action, ""];
                  return (
                    <tr key={d.id}>
                      <td className="muted">{shortDT(d.bar_time ?? d.ts)}</td>
                      <td className="sym">{d.symbol}</td>
                      <td className="r">{price(d.close)}</td>
                      <td className="r">{price(d.sma_short)}</td>
                      <td className="r">{price(d.sma_long)}</td>
                      <td className={d.signal === "buy" ? "side-buy" : d.signal === "sell" ? "side-sell" : "muted"}>{d.signal === "buy" ? "Kauf" : d.signal === "sell" ? "Verkauf" : "—"}</td>
                      <td><Tag tone={tn}>{label}</Tag></td>
                      <td className="wrap">{d.reason}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
