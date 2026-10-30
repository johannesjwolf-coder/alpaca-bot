import { useEffect, useMemo, useState } from "react";
import { api, errorText } from "../api";
import { useBotInfo } from "../components/BotCard";
import { EquityChart } from "../components/chart/EquityChart";
import { TradingChart, type ChartMarker } from "../components/chart/TradingChart";
import { SymbolPicker } from "../components/SymbolPicker";
import { Card, EmptyState, Icon, Kpi, Notice, Seg, Spinner, Tag } from "../components/ui";
import { amount2, dateTime, localTz, money, num, pct, price, shortDT, signedMoney, tone } from "../format";
import { usePoll } from "../hooks";
import type { BacktestRequest, BacktestResult, BacktestRun, Status } from "../types";

const DEFAULT: BacktestRequest = {
  symbol: "BTC/USD", strategy: "scalp", days: 3, short_window: 1, long_window: 3, order_value: 50,
  stop_loss_pct: 0.3, take_profit_pct: 0.6, max_hold_min: 10, fee_pct: 0.25, spread_pct: 0.05, apply_risk_limits: true,
};
type NumKey = "short_window" | "long_window" | "order_value" | "stop_loss_pct" | "take_profit_pct" | "max_hold_min" | "fee_pct" | "spread_pct";
const FIELDS: [NumKey, string, string, boolean][] = [
  ["short_window", "Kurzer Ø (Minuten)", "1", true],
  ["long_window", "Langer Ø (Minuten)", "1", true],
  ["order_value", "Einsatz je Trade (USD)", "1", true],
  ["stop_loss_pct", "Stop-Loss (%)", "0.05", false],
  ["take_profit_pct", "Take-Profit (%)", "0.05", false],
  ["max_hold_min", "Max. Haltedauer (Min.)", "1", false],
  ["fee_pct", "Gebühr je Ausführung (%)", "0.01", true],
  ["spread_pct", "Angenommener Spread (%)", "0.01", true],
];

function validate(r: BacktestRequest): Partial<Record<keyof BacktestRequest, string>> {
  const e: Partial<Record<keyof BacktestRequest, string>> = {};
  const crypto = r.symbol.includes("/");
  if (r.strategy === "scalp" && !crypto) e.symbol = "Scalping ist für Krypto vorgesehen (z. B. BTC/USD).";
  if (r.strategy === "sma" && crypto) e.symbol = "Die SMA-Strategie ist für US-Aktien/ETFs vorgesehen.";
  if (!(r.short_window >= 1)) e.short_window = "Mindestens 1.";
  if (!(r.long_window > r.short_window)) e.long_window = "Muss grösser als der kurze Ø sein.";
  if (!(r.order_value > 0)) e.order_value = "Muss grösser als 0 sein.";
  if (r.strategy === "scalp") {
    if (!(r.stop_loss_pct > 0 && r.stop_loss_pct <= 20)) e.stop_loss_pct = "Zwischen 0 und 20 %.";
    if (!(r.take_profit_pct > 0 && r.take_profit_pct <= 50)) e.take_profit_pct = "Zwischen 0 und 50 %.";
    if (!(r.max_hold_min > 0)) e.max_hold_min = "Muss grösser als 0 sein.";
  }
  if (!(r.fee_pct >= 0 && r.fee_pct <= 2)) e.fee_pct = "Zwischen 0 und 2 %.";
  if (!(r.spread_pct >= 0 && r.spread_pct <= 5)) e.spread_pct = "Zwischen 0 und 5 %.";
  return e;
}

export function BacktestPage({ status }: { status: Status }) {
  const mode = status.mode;
  const bot = useBotInfo(mode, 60_000);
  const runs = usePoll(() => api.get<{ runs: BacktestRun[] }>("/backtests"), 60_000, [mode]);
  const [req, setReq] = useState<BacktestRequest>(DEFAULT);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const errors = touched ? validate(req) : {};

  function fromBot() {
    const p = bot.data?.params;
    if (!p) return;
    const toMin = (n: number) => Math.max(1, Math.round((n * p.bar_seconds) / 60));
    const short = p.strategy === "scalp" ? toMin(p.short_window) : p.short_window;
    const long = p.strategy === "scalp" ? Math.max(short + 1, toMin(p.long_window)) : p.long_window;
    setReq((r) => ({
      ...r, symbol: p.symbol, strategy: p.strategy, short_window: short, long_window: long, order_value: p.order_value,
      stop_loss_pct: p.stop_loss_pct, take_profit_pct: p.take_profit_pct, max_hold_min: p.max_hold_min, fee_pct: p.fee_pct,
      spread_pct: p.strategy === "scalp" ? 0.05 : 0.02,
    }));
  }
  // Beim ersten Laden die aktuellen Bot-Parameter übernehmen
  useEffect(() => {
    if (bot.data && req === DEFAULT) fromBot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bot.data]);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (Object.keys(validate(req)).length) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<BacktestResult>("/backtests", { ...req, expected_mode: mode });
      setResult(res);
      void runs.reload();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function load(id: number) {
    setError(null);
    try {
      setResult(await api.get<BacktestResult>(`/backtests/${id}`));
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (err) {
      setError(errorText(err));
    }
  }

  const set = <K extends keyof BacktestRequest>(k: K, v: BacktestRequest[K]) => setReq((r) => ({ ...r, [k]: v }));
  const m = result?.metrics;
  const markers: ChartMarker[] = useMemo(() => (result?.trades ?? []).flatMap((t) => [
    { time: t.entry_time, side: "buy" as const, label: `Einstieg ${num(t.qty, 8)} zu ${price(t.entry_price)}` },
    { time: t.exit_time, side: "sell" as const, label: `Ausstieg (${t.reason}) zu ${price(t.exit_price)} · netto ${signedMoney(t.net, "USD")}` },
  ]), [result]);

  return (
    <>
      <div className="workspace">
        <div className="stack" style={{ gap: 16 }}>
          {error && <Notice tone="error" title="Backtest fehlgeschlagen">{error}</Notice>}
          {!result && !busy && (
            <Card><EmptyState icon="backtest" title="Noch kein Ergebnis">
              Parameter links prüfen und „Backtest starten“. Gerechnet wird auf historischen 1-Minuten-Kerzen
              {mode === "DEMO" ? " (DEMO: synthetische Daten)." : " von Alpaca."} Es werden keine Orders gesendet.
            </EmptyState></Card>
          )}
          {busy && <Card><div className="stack"><div className="skeleton" style={{ height: 112 }} /><div className="skeleton" style={{ height: 340 }} /></div></Card>}
          {result && m && !busy && (
            <>
              <div className="kpis">
                <Kpi label="Netto-Ergebnis" value={<span className={tone(m.net_pnl)}>{m.net_pnl > 0 ? "+" : m.net_pnl < 0 ? "−" : ""}{amount2(Math.abs(m.net_pnl))}</span>} unit="USD"
                     sub={<span>{m.trades} Trades · Kosten {money(m.fees, "USD")}</span>} />
                <Kpi label="Trefferquote" value={m.win_rate_pct !== null ? `${num(m.win_rate_pct, 1)} %` : "—"}
                     sub={<span><span className="pos">{m.wins} Gewinner</span> · <span className="neg">{m.losses} Verlierer</span></span>} />
                <Kpi label="Profit-Faktor" value={m.profit_factor !== null ? num(m.profit_factor, 2) : "—"}
                     sub={<span>Erwartung je Trade {signedMoney(m.expectancy, "USD")}</span>} />
                <Kpi label="Max. Drawdown" value={<span className={m.max_drawdown > 0 ? "neg" : ""}>{m.max_drawdown > 0 ? "−" : ""}{amount2(m.max_drawdown)}</span>} unit="USD"
                     sub={<span>Buy &amp; Hold im Zeitraum {pct(m.buy_hold_pct)}</span>} />
              </div>
              {result.warning && <Notice tone="warn">{result.warning}</Notice>}
              <section className="card chart-card">
                <div className="chart-toolbar">
                  <h2 style={{ fontSize: 15 }}>{result.request.symbol}</h2>
                  <Tag>{result.request.strategy === "scalp" ? "Scalping" : "SMA"}</Tag>
                  <Tag>{result.display_timeframe_min > 1 ? `${result.display_timeframe_min}-Min.-Kerzen (Anzeige)` : "1-Min.-Kerzen"}</Tag>
                  {result.mode === "DEMO" && <Tag tone="gold">DEMO-Daten</Tag>}
                  <span className="spacer" />
                  <span className="muted small">{dateTime(result.period.start)} – {dateTime(result.period.end)}</span>
                </div>
                <div className="chart-area compact">
                  {result.bars.length > 1 ? (
                    <TradingChart bars={result.bars} chartType="candles" showVolume={false}
                                  mas={[]} markers={markers} timeframeSec={result.display_timeframe_min * 60}
                                  title={`${result.request.symbol} · Backtest`} resetKey={`bt-${result.id}`} />
                  ) : <div className="chart-overlay"><EmptyState title="Keine Kerzen im Zeitraum" /></div>}
                </div>
                <div className="chart-foot"><span>Quelle: {result.source}</span><span>{num(result.bar_count, 0)} Kerzen ausgewertet</span><span>K = Einstieg, V = Ausstieg (Details beim Überfahren)</span></div>
              </section>
              <Card title="Equity-Kurve (netto, inkl. offener Position)" bodyClass="card-body tight">
                <EquityChart points={result.equity} />
              </Card>
              <div className="grid-2">
                <Card title="Kennzahlen">
                  <dl className="kv">
                    <dt>Brutto / Kosten / Netto</dt><dd>{signedMoney(m.gross_pnl, "USD")} / {money(m.fees, "USD")} / <strong className={tone(m.net_pnl)}>{signedMoney(m.net_pnl, "USD")}</strong></dd>
                    <dt>Ø Gewinn / Ø Verlust</dt><dd><span className="pos">{signedMoney(m.avg_win, "USD")}</span> / <span className="neg">{signedMoney(m.avg_loss, "USD")}</span></dd>
                    <dt>Netto in % des Einsatzes</dt><dd className={tone(m.net_pct_of_order_value)}>{pct(m.net_pct_of_order_value)}</dd>
                    <dt>Ø Haltedauer</dt><dd>{m.avg_hold_min !== null ? `${num(m.avg_hold_min, 1)} Min.` : "—"}</dd>
                    <dt>Zeit im Markt</dt><dd>{m.exposure_pct !== null ? `${num(m.exposure_pct, 1)} %` : "—"}</dd>
                    <dt>Ausstiege</dt><dd>{Object.entries(m.exit_reasons).map(([k, v]) => `${k}: ${v}`).join(" · ") || "—"}</dd>
                    <dt>Gesperrte Einstiege</dt><dd>{Object.entries(m.blocked_entries).map(([k, v]) => `${k}: ${v}`).join(" · ") || "keine"}</dd>
                  </dl>
                </Card>
                <Card title="Annahmen">
                  <ul className="sm" style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4 }}>
                    {result.assumptions.map((a) => <li key={a}>{a}</li>)}
                    <li>Risikogrenzen: {result.request.apply_risk_limits ? "nachgebildet (Einstiege pro Stunde/Tag, Verlustserie, Tagesverlust, Orderwert)" : "nicht angewendet"}.</li>
                  </ul>
                </Card>
              </div>
              <Card title="Trades" hint={`${result.trades.length} angezeigt`} bodyClass={null}>
                {!result.trades.length ? <EmptyState title="Keine Trades im Zeitraum">Mit diesen Parametern gab es kein Einstiegssignal.</EmptyState> : (
                  <div className="table-wrap" style={{ maxHeight: 420 }}>
                    <table className="data">
                      <thead><tr><th>Einstieg ({localTz})</th><th className="r">Kurs</th><th>Ausstieg</th><th className="r">Kurs</th><th>Grund</th><th className="r">Dauer</th><th className="r">Brutto</th><th className="r">Kosten</th><th className="r">Netto</th></tr></thead>
                      <tbody>
                        {[...result.trades].reverse().map((t, i) => (
                          <tr key={i}>
                            <td className="muted">{shortDT(t.entry_time)}</td>
                            <td className="r">{price(t.entry_price)}</td>
                            <td className="muted">{shortDT(t.exit_time)}</td>
                            <td className="r">{price(t.exit_price)}</td>
                            <td>{t.reason}</td>
                            <td className="r">{num(t.hold_min, 1)} Min.</td>
                            <td className={`r ${tone(t.gross)}`}>{signedMoney(t.gross, "USD")}</td>
                            <td className="r muted">{money(t.fees, "USD")}</td>
                            <td className={`r ${tone(t.net)}`}><strong>{signedMoney(t.net, "USD")}</strong></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </>
          )}
        </div>

        <div className="side-stack">
          <Card title="Backtest einstellen" hint={mode === "DEMO" ? "DEMO-Daten" : "Alpaca-Marktdaten"}>
            <form className="stack" onSubmit={run} noValidate>
              <div className={`field ${errors.symbol ? "invalid" : ""}`}>
                <span>Symbol</span>
                <SymbolPicker value={req.symbol} onChange={(s) => setReq((r) => ({ ...r, symbol: s, strategy: s.includes("/") ? "scalp" : "sma", spread_pct: s.includes("/") ? 0.05 : 0.02 }))} mode={mode} label="Symbol für Backtest" />
                {errors.symbol && <span className="field-error">{errors.symbol}</span>}
              </div>
              <div className="field">
                <span>Strategie</span>
                <Seg label="Strategie" className="block" value={req.strategy} onChange={(v) => set("strategy", v)}
                     options={[{ id: "scalp", label: "Scalping Krypto" }, { id: "sma", label: "SMA Aktien" }]} />
              </div>
              <div className="field">
                <span>Zeitraum</span>
                <Seg label="Zeitraum" className="block" value={String(req.days) as "1" | "3" | "7" | "14"} onChange={(v) => set("days", Number(v))}
                     options={[{ id: "1", label: "1 Tag" }, { id: "3", label: "3 Tage" }, { id: "7", label: "7 Tage" }, { id: "14", label: "14 Tage" }]} />
              </div>
              <div className="form-grid">
                {FIELDS.filter(([, , , always]) => always || req.strategy === "scalp").map(([k, label, step]) => (
                  <label key={k} className={`field ${errors[k] ? "invalid" : ""}`}>
                    <span>{label}</span>
                    <input type="number" step={step} min={0} value={req[k]} onChange={(e) => set(k, Number(e.target.value))} aria-invalid={!!errors[k]} />
                    {errors[k] && <span className="field-error">{errors[k]}</span>}
                  </label>
                ))}
              </div>
              <label className="check">
                <input type="checkbox" checked={req.apply_risk_limits} onChange={(e) => set("apply_risk_limits", e.target.checked)} />
                <span>Risikogrenzen des {mode}-Modus nachbilden (Einstiege pro Stunde/Tag, Verlustserie, Tagesverlust)</span>
              </label>
              <p className="field-help">
                Live rechnet der Scalper mit 10-s-Mikrokerzen; historisch gibt es 1-Minuten-Kerzen. „Aus Bot übernehmen“ rechnet
                die Durchschnitte in Minuten um.
              </p>
              <div className="row">
                <button className="btn primary" type="submit" disabled={busy}>{busy ? <Spinner /> : <Icon name="play" size={16} />} Backtest starten</button>
                <button className="btn" type="button" onClick={fromBot} disabled={!bot.data}>Aus Bot übernehmen</button>
              </div>
            </form>
          </Card>

          <Card title="Gespeicherte Läufe" hint="Zum Vergleichen" bodyClass={null}>
            {!runs.data?.runs.length ? <EmptyState title="Noch keine Läufe" /> : (
              <ul className="list" style={{ maxHeight: 460, overflowY: "auto" }}>
                {runs.data.runs.map((r) => (
                  <li key={r.id} className={`list-item ${result?.id === r.id ? "selected" : ""}`}>
                    <div>
                      <span className="sym">{r.symbol}</span>{" "}
                      <span className="meta">{r.request.days} T · Ø {r.request.short_window}/{r.request.long_window}{r.strategy === "scalp" ? ` · SL/TP ${num(r.request.stop_loss_pct, 2)}/${num(r.request.take_profit_pct, 2)} %` : " · SMA"}</span>
                      <div className="meta">{shortDT(r.created_at)} · {r.metrics.trades} Trades{r.metrics.win_rate_pct !== null ? ` · ${num(r.metrics.win_rate_pct, 0)} % Treffer` : ""}</div>
                    </div>
                    <div className="val">
                      <strong className={tone(r.metrics.net_pnl)}>{signedMoney(r.metrics.net_pnl, "USD")}</strong>
                      <div><button className="btn sm ghost" onClick={() => void load(r.id)} aria-label={`Lauf ${r.id} anzeigen`}>Anzeigen</button></div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
