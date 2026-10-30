import { useState } from "react";
import { api, errorText } from "../api";
import { money, pct, price, signedMoney, tone } from "../format";
import { usePoll } from "../hooks";
import type { BotInfo, Status } from "../types";
import { Icon, Notice, Pill, Spinner, useConfirm } from "./ui";

export function useBotInfo(mode: string, intervalMs = 3000) {
  return usePoll(() => api.get<BotInfo>("/bot?limit=100"), intervalMs, [mode]);
}

/** Kompakte Bot-Steuerung für die Übersicht. */
export function BotCard({ status, onChange, onNavigate }: { status: Status; onChange: () => void; onNavigate: (p: string) => void }) {
  const info = useBotInfo(status.mode);
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const running = status.bot.running;
  const params = info.data?.params;
  const live = info.data?.state.scalper;
  const summary = info.data?.summary;
  const scalp = params?.strategy === "scalp";
  const views = Object.entries(live?.symbols ?? {});
  const openTrades = views.filter(([, v]) => v.trade);

  async function start() {
    if (!params) return;
    const ok = await confirm({
      title: "Automatik starten?",
      confirmLabel: "Bot starten",
      body: (
        <dl className="kv">
          <dt>Modus</dt><dd><strong>{status.mode}</strong></dd>
          <dt>Strategie</dt><dd>{params.strategy === "scalp" ? "Scalping Krypto" : "SMA Aktien (Minuten)"}</dd>
          <dt>{params.strategy === "scalp" ? "Symbole" : "Symbol"}</dt><dd>{params.strategy === "scalp" ? params.symbols.join(", ") : params.symbol}</dd>
          <dt>Einsatz je Trade</dt><dd>{money(params.order_value, "USD")}{params.strategy === "scalp" && params.symbols.length > 1 ? " je Symbol" : ""}</dd>
          {params.strategy === "scalp" && <><dt>Stop-Loss / Ziel</dt><dd>−{params.stop_loss_pct} % / +{params.take_profit_pct} %</dd></>}
          {params.strategy === "scalp" && <><dt>Schutzorder</dt><dd>{params.broker_stop ? "Stop-Limit beim Broker" : "aus"}</dd></>}
        </dl>
      ),
    });
    if (!ok) return;
    await act(() => api.post("/bot/start", { expected_mode: status.mode }));
  }
  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(false);
      onChange();
      void info.reload();
    }
  }

  return (
    <section className="card" aria-label="Bot">
      <div className="card-head">
        <h2>Automatik</h2>
        <span style={{ marginLeft: "auto" }}>
          <Pill tone={running ? "ok" : ""} running={running}>{running ? "läuft" : "pausiert"}</Pill>
        </span>
      </div>
      <div className="card-body stack">
        {params ? (
          <dl className="kv">
            <dt>Strategie</dt><dd>{params.strategy === "scalp" ? "Scalping Krypto" : "SMA Aktien (Minuten)"}</dd>
            <dt>{scalp ? "Symbole" : "Symbol"}</dt><dd className="sym">{scalp ? params.symbols.join(", ") : params.symbol}</dd>
            {running && <><dt>Status</dt><dd>{live?.status || status.bot.last_status || "—"}</dd></>}
            {summary && (
              <>
                <dt>Heute</dt>
                <dd>{summary.trades_today} Trades · <span className={tone(summary.net_today)}>{signedMoney(summary.net_today, "USD")}</span> netto</dd>
              </>
            )}
          </dl>
        ) : (
          <div className="skeleton" style={{ height: 96 }} />
        )}
        {running && views.length > 0 && (
          <ul className="mini-quotes" aria-label="Kurse je Symbol">
            {views.map(([sym, v]) => (
              <li key={sym}>
                <span className="sym">{sym}</span>
                <span className="num">{v.quote ? `${price(v.quote.bid)} / ${price(v.quote.ask)}` : "—"}</span>
                {v.trade
                  ? <span className={`num ${tone(v.trade.pnl_pct)}`}>{pct(v.trade.pnl_pct ?? null, 2)}</span>
                  : <span className="muted small">{v.quote && !v.quote.fresh ? "veraltet" : "flach"}</span>}
              </li>
            ))}
          </ul>
        )}
        {openTrades.map(([sym, v]) => v.trade && (
          <Notice key={sym} title={`${sym}: Position seit ${price(v.trade.entry_price)}`}>
            Aktuell {price(v.trade.bid ?? null)} · <span className={tone(v.trade.pnl_pct)}>{pct(v.trade.pnl_pct ?? null, 3)}</span>
            <br />Stop {price(v.trade.stop_price)}{v.trade.broker_stop ? " (Schutzorder)" : ""} · Ziel {price(v.trade.target_price)}
          </Notice>
        ))}
        {summary?.loss_pause && <Notice tone="warn" title="Kaufpause nach Verlustserie">{summary.loss_pause.reason}</Notice>}
        {status.mode === "PAPER" && !status.orders_enabled && !running && (
          <p className="field-help">Start im PAPER-Modus erst nach Paper-Orderfreigabe.</p>
        )}
        <div className="row">
          {running ? (
            <button className="btn" disabled={busy} onClick={() => act(() => api.post("/bot/pause"))}
                    title="Stoppt neue automatische Aufträge inkl. automatischer Ausstiege. Positionen und Schutzorders beim Broker bleiben bestehen.">
              {busy ? <Spinner /> : <Icon name="pause" size={16} />} Pausieren
            </button>
          ) : (
            <button className="btn primary" disabled={busy || (status.mode === "PAPER" && !status.orders_enabled)} onClick={start}>
              {busy ? <Spinner /> : <Icon name="play" size={16} />} Bot starten
            </button>
          )}
          <button className="btn ghost" onClick={() => onNavigate("bot")}>Details</button>
        </div>
        <p className="field-help">Pause stoppt neue automatische Aufträge inkl. Take-Profit/Haltedauer. Positionen und gesetzte Schutzorders bleiben bestehen.</p>
        {msg && <Notice tone="error">{msg}</Notice>}
      </div>
    </section>
  );
}
