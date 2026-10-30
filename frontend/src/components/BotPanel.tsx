import { useEffect, useState } from "react";
import { api, errorText } from "../api";
import { ago, dateTime, money, num, signedMoney } from "../format";
import { usePoll } from "../hooks";
import type { BotInfo, BotParams, ScalperState, Status, Trade } from "../types";

const ACTION_LABEL: Record<string, [string, string]> = {
  none: ["Keine Aktion", "muted"],
  skip: ["Übersprungen", "warn"],
  blocked: ["Gesperrt (Risiko)", "bad"],
  failed: ["Broker lehnte ab", "bad"],
  order: ["Order übermittelt", "ok"],
};
const SIGNAL_LABEL: Record<string, string> = { buy: "Kauf", sell: "Verkauf", none: "—" };
const TRADE_STATUS: Record<Trade["status"], [string, string]> = {
  entering: ["Einstieg läuft", "warn"],
  open: ["Offen", "info"],
  exiting: ["Ausstieg läuft", "warn"],
  closed: ["Geschlossen", "muted"],
  canceled: ["Abgebrochen", "muted"],
};

type NumKey = Exclude<keyof BotParams, "strategy" | "symbol">;
const SCALP_FIELDS: [NumKey, string, string][] = [
  ["order_value", "Einsatz je Trade (USD)", "1"],
  ["stop_loss_pct", "Stop-Loss (%)", "0.05"],
  ["take_profit_pct", "Take-Profit (%)", "0.05"],
  ["max_hold_min", "Max. Haltedauer (Min.)", "1"],
  ["bar_seconds", "Mikrokerze (Sek.)", "1"],
  ["short_window", "Kurzer Ø (Mikrokerzen)", "1"],
  ["long_window", "Langer Ø (Mikrokerzen)", "1"],
  ["poll_interval_s", "Kursabruf alle (Sek.)", "0.5"],
  ["fee_pct", "Gebühr je Ausführung (%)", "0.01"],
];
const SMA_FIELDS: [NumKey, string, string][] = [
  ["order_value", "Zielwert je Kauf", "1"],
  ["short_window", "Kurzer SMA (Minutenkerzen)", "1"],
  ["long_window", "Langer SMA (Minutenkerzen)", "1"],
  ["max_bar_age_s", "Max. Kerzenalter (s)", "10"],
];

export function BotPanel({ status, onChange }: { status: Status; onChange: () => void }) {
  const mode = status.mode;
  const info = usePoll(() => api.get<BotInfo>("/bot?limit=100"), 3000, [mode]);
  const [params, setParams] = useState<BotParams | null>(null);
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
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
      setMsg({ tone: "bad", text: errorText(e) });
    } finally {
      setBusy(false);
      onChange();
      void info.reload();
    }
  }

  const setNum = (k: NumKey, v: string) => setParams((p) => (p ? { ...p, [k]: Number(v) } : p));
  const scalp = params?.strategy === "scalp";
  const cost = params ? 2 * params.fee_pct : 0;
  const live = info.data?.state.scalper ?? status.bot.scalper;
  const summary = info.data?.summary;

  return (
    <>
      <div className="grid cols-2">
        <div className="card">
          <div className="card-head">
            <h2>Automatik</h2>
            <span className={`badge ${running ? "ok running" : "muted"}`}>
              <span className="dot" /> {running ? `läuft · ${status.bot.strategy === "scalp" ? "Scalping" : "SMA"}` : "pausiert"}
            </span>
          </div>
          <div className="stack">
            <div className="row">
              <button
                className="btn primary"
                disabled={busy || running || (mode === "PAPER" && !status.orders_enabled)}
                onClick={() => {
                  if (mode === "PAPER" && !window.confirm("Automatik im PAPER-Modus starten? Der Bot sendet dann selbstständig Paper-Orders innerhalb der Risikogrenzen.")) return;
                  void act(() => api.post("/bot/start", { expected_mode: mode }), "Bot gestartet.");
                }}
              >
                Start
              </button>
              <button className="btn" disabled={busy || !running}
                      onClick={() => void act(() => api.post("/bot/pause"), "Bot pausiert.")}>
                Pause
              </button>
              <span className="small muted">{status.bot.last_status || "—"}</span>
            </div>
            <div className="notice info small">
              <strong>Was „Pause“ bedeutet</strong>
              Pausieren stoppt alle <em>neuen automatischen</em> Aufträge – beim Scalping auch die automatischen
              Ausstiege (Stop-Loss, Take-Profit, Haltedauer). Offene Positionen bleiben bestehen. Zum Aussteigen:
              „Position schliessen…“ in der Übersicht oder <b>Not-Aus</b> oben rechts.
            </div>
            {mode === "PAPER" && !status.orders_enabled && (
              <div className="notice warn small">Start im PAPER-Modus erst nach ausdrücklicher Paper-Orderfreigabe (Einstellungen).</div>
            )}
            {summary && (
              <div className="row small">
                <span>Trades heute: <strong>{summary.trades_today}</strong></span>
                <span className="tone-ok">Gewinner {summary.wins_today}</span>
                <span className="tone-bad">Verlierer {summary.losses_today}</span>
                <span>Netto (geschätzt): <strong className={summary.net_today < 0 ? "tone-bad" : "tone-ok"}>{signedMoney(summary.net_today, "USD")}</strong></span>
              </div>
            )}
            {summary?.loss_pause && (
              <div className="notice warn small">
                <strong>Kaufpause nach Verlustserie</strong>
                {summary.loss_pause.reason} – neue Käufe bis {dateTime(summary.loss_pause.until)} gesperrt. Verkäufe bleiben möglich.
              </div>
            )}
            {msg && <div className={`notice ${msg.tone}`}>{msg.text}</div>}
          </div>
        </div>

        {running && status.bot.strategy === "scalp" && live ? (
          <LiveScalper live={live} />
        ) : (
          <div className="card">
            <div className="card-head"><h2>Live-Status</h2></div>
            <div className="muted small">
              Wird angezeigt, solange der Scalping-Bot läuft: Geld-/Briefkurs, Spread, Aufwärmphase, offene Position
              mit Stop-Loss und Take-Profit.
            </div>
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Strategie und Parameter</h2>
          {running && <span className="small muted">Änderungen nur bei pausiertem Bot.</span>}
        </div>
        {params ? (
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              void act(() => api.put("/settings/bot", { ...params, expected_mode: mode }), "Parameter gespeichert.");
            }}
          >
            <div className="row">
              <div className="segmented" role="radiogroup" aria-label="Strategie">
                <button type="button" className={scalp ? "active" : ""} disabled={running}
                        onClick={() => info.data && setParams({ ...info.data.defaults.scalp })}>
                  Scalping Krypto
                </button>
                <button type="button" className={!scalp ? "active" : ""} disabled={running}
                        onClick={() => info.data && setParams({ ...info.data.defaults.sma })}>
                  SMA Aktien (Minuten)
                </button>
              </div>
              <span className="small muted">Beim Umschalten werden die Standardwerte der Strategie eingesetzt.</span>
            </div>
            <div className="form-grid">
              <label className="field">
                Symbol {scalp ? "(Krypto, z. B. BTC/USD)" : "(US-Aktie/ETF)"}
                <input value={params.symbol} disabled={running}
                       onChange={(e) => setParams({ ...params, symbol: e.target.value.toUpperCase() })} />
              </label>
              {(scalp ? SCALP_FIELDS : SMA_FIELDS).map(([k, label, step]) => (
                <label className="field" key={k}>
                  {label}
                  <input type="number" step={step} min={0} value={params[k]} disabled={running}
                         onChange={(e) => setNum(k, e.target.value)} />
                </label>
              ))}
            </div>
            {scalp && (
              <div className={`notice small ${params.take_profit_pct <= cost ? "warn" : "info"}`}>
                <strong>Kostenrechnung je Trade (geschätzt)</strong>
                Gebühren für Kauf und Verkauf ≈ {num(cost, 2)} % plus Spread. Take-Profit {num(params.take_profit_pct, 2)} % →
                netto höchstens ≈ {num(params.take_profit_pct - cost, 2)} %; Stop-Loss {num(params.stop_loss_pct, 2)} % →
                netto ≈ −{num(params.stop_loss_pct + cost, 2)} %.
                {params.take_profit_pct <= cost && " Das Ziel liegt nicht über den Kosten – netto ist kein Gewinn möglich."}
              </div>
            )}
            <div className="row">
              <button className="btn" type="submit" disabled={busy || running}>Speichern</button>
            </div>
            <div className="small muted">
              {scalp
                ? "Scalping-Teststrategie: Kurs alle paar Sekunden, Mikrokerzen, Einstieg bei Aufwärtskreuzung der Durchschnitte, Ausstieg per Stop-Loss, Take-Profit oder Haltedauer. Keine validierte Strategie, keine Gewinnzusage."
                : info.data?.disclaimer}
            </div>
          </form>
        ) : (
          <div className="muted">Lade…</div>
        )}
      </div>

      <TradesTable trades={info.data?.trades ?? []} />

      <div className="card">
        <div className="card-head">
          <h2>Entscheidungsverlauf</h2>
          <span className="small muted">Einstiege, Ausstiege und gesperrte Signale mit Begründung.</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Zeit</th>
                <th>Symbol</th>
                <th className="num">Kurs</th>
                <th className="num">Ø kurz</th>
                <th className="num">Ø lang</th>
                <th>Signal</th>
                <th>Aktion</th>
                <th>Begründung</th>
              </tr>
            </thead>
            <tbody>
              {(!info.data || info.data.decisions.length === 0) && (
                <tr><td colSpan={8} className="empty">Noch keine Entscheidungen ({mode})</td></tr>
              )}
              {info.data?.decisions.map((d) => {
                const [label, tone] = ACTION_LABEL[d.action] ?? [d.action, "muted"];
                return (
                  <tr key={d.id}>
                    <td className="small">{dateTime(d.bar_time ?? d.ts)}</td>
                    <td>{d.symbol}</td>
                    <td className="num">{num(d.close, 2)}</td>
                    <td className="num">{num(d.sma_short, 3)}</td>
                    <td className="num">{num(d.sma_long, 3)}</td>
                    <td className={d.signal === "buy" ? "tone-ok" : d.signal === "sell" ? "tone-warn" : "muted"}>
                      {SIGNAL_LABEL[d.signal]}
                    </td>
                    <td className={`tone-${tone}`}>{label}</td>
                    <td className="small">{d.reason}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function LiveScalper({ live }: { live: ScalperState }) {
  const q = live.quote;
  const t = live.trade;
  const remaining = t?.deadline ? Math.max(0, Math.round((new Date(t.deadline).getTime() - Date.now()) / 1000)) : null;
  return (
    <div className="card">
      <div className="card-head">
        <h2>Live: {live.symbol}</h2>
        {q && <span className={`badge ${q.fresh ? "ok" : "warn"}`}>{q.fresh ? "Kurs frisch" : "Kurs veraltet"}</span>}
      </div>
      <dl className="kv">
        <dt>Status</dt><dd>{live.status ?? "—"}</dd>
        <dt>Geld / Brief</dt>
        <dd>{q ? `${money(q.bid, "USD")} / ${money(q.ask, "USD")}` : "—"}</dd>
        <dt>Spread</dt><dd>{q ? `${num(q.spread_pct, 3)} %` : "—"}</dd>
        <dt>Kurszeit</dt><dd>{q ? `${dateTime(q.at)} (${ago(q.at)})` : "—"}</dd>
        {live.bars_needed !== undefined && live.bars_ready !== undefined && live.bars_ready < live.bars_needed && (
          <><dt>Aufwärmen</dt><dd>{live.bars_ready}/{live.bars_needed} Mikrokerzen</dd></>
        )}
        {live.last_eval && (
          <><dt>Letzte Auswertung</dt>
          <dd>Ø kurz {num(live.last_eval.sma_short, 2)} · Ø lang {num(live.last_eval.sma_long, 2)}</dd></>
        )}
      </dl>
      {t && (
        <div className="notice info small" style={{ marginTop: 12 }}>
          <strong>Offene Position #{t.id}: {num(t.qty, 8)} zu {money(t.entry_price, "USD")}</strong>
          Aktuell (Geldkurs) {money(t.bid ?? null, "USD")} ·{" "}
          <span className={(t.pnl_pct ?? 0) < 0 ? "tone-bad" : "tone-ok"}>{t.pnl_pct !== undefined ? `${num(t.pnl_pct, 3)} %` : "—"}</span>
          <br />
          Stop-Loss {money(t.stop_price, "USD")} · Take-Profit {money(t.target_price, "USD")} · Restzeit{" "}
          {remaining !== null ? `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")} Min.` : "—"}
        </div>
      )}
    </div>
  );
}

function TradesTable({ trades }: { trades: Trade[] }) {
  return (
    <div className="card">
      <div className="card-head">
        <h2>Trades (Einstieg → Ausstieg)</h2>
        <span className="small muted">Kosten sind geschätzt (Gebühr je Ausführung); massgeblich ist die Abrechnung des Brokers.</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Einstieg</th>
              <th>Symbol</th>
              <th className="num">Menge</th>
              <th className="num">Einstiegskurs</th>
              <th className="num">Ausstiegskurs</th>
              <th>Grund</th>
              <th className="num">Brutto</th>
              <th className="num">Kosten</th>
              <th className="num">Netto</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 && <tr><td colSpan={10} className="empty">Noch keine Trades</td></tr>}
            {trades.map((t) => {
              const [label, tone] = TRADE_STATUS[t.status];
              return (
                <tr key={t.id}>
                  <td className="small">{dateTime(t.entry_time ?? t.created_at)}</td>
                  <td>{t.symbol}</td>
                  <td className="num">{num(t.entry_qty, 8)}</td>
                  <td className="num">{money(t.entry_price, "USD")}</td>
                  <td className="num">{money(t.exit_price, "USD")}</td>
                  <td className="small">{t.exit_reason ?? "—"}</td>
                  <td className="num">{signedMoney(t.gross_pnl, "USD")}</td>
                  <td className="num muted">{money(t.fees_est, "USD")}</td>
                  <td className={`num ${t.net_pnl === null ? "" : t.net_pnl < 0 ? "tone-bad" : "tone-ok"}`}>
                    {signedMoney(t.net_pnl, "USD")}
                  </td>
                  <td className={`small tone-${tone}`}>{label}{t.note ? <div className="muted">{t.note}</div> : null}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
