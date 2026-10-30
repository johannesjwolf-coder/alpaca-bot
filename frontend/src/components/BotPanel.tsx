import { useEffect, useState } from "react";
import { api, errorText } from "../api";
import { dateTime, num } from "../format";
import { usePoll } from "../hooks";
import type { BotInfo, BotParams, Status } from "../types";

const ACTION_LABEL: Record<string, [string, string]> = {
  none: ["Keine Aktion", "muted"],
  skip: ["Übersprungen", "warn"],
  blocked: ["Gesperrt (Risiko)", "bad"],
  failed: ["Broker lehnte ab", "bad"],
  order: ["Order übermittelt", "ok"],
};
const SIGNAL_LABEL: Record<string, string> = { buy: "Kaufsignal", sell: "Verkaufssignal", none: "—" };

export function BotPanel({ status, onChange }: { status: Status; onChange: () => void }) {
  const mode = status.mode;
  const info = usePoll(() => api.get<BotInfo>("/bot?limit=100"), 5000, [mode]);
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

  const set = (k: keyof BotParams, v: string) =>
    setParams((p) => (p ? { ...p, [k]: k === "symbol" ? v.toUpperCase() : Number(v) } : p));

  return (
    <>
      <div className="grid cols-2">
        <div className="card">
          <div className="card-head">
            <h2>Automatik</h2>
            <span className={`badge ${running ? "ok running" : "muted"}`}>
              <span className="dot" /> {running ? "läuft" : "pausiert"}
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
              <span className="small muted">Status: {status.bot.last_status || "—"}</span>
            </div>
            <div className="notice info small">
              <strong>Was „Pause“ bedeutet</strong>
              Pausieren stoppt nur <em>neue automatische</em> Aufträge. Bestehende Positionen bleiben bestehen und
              bereits gesendete Broker-Orders bleiben aktiv. Stornieren und Schliessen sind separate, ausdrückliche
              Aktionen in der Übersicht.
            </div>
            {mode === "PAPER" && !status.orders_enabled && (
              <div className="notice warn small">
                Start im PAPER-Modus erst nach ausdrücklicher Paper-Orderfreigabe (Einstellungen).
              </div>
            )}
            <div className="small muted">
              Startbedingungen: gültige Parameter, erfolgreicher Kontoabgleich, Markt offen, Brokerstatus aktiv,
              frische abgeschlossene Minutenkerze. Nach jedem Neustart ist die Automatik aus. Es läuft genau eine
              Bot-Instanz.
            </div>
            {msg && <div className={`notice ${msg.tone}`}>{msg.text}</div>}
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Strategieparameter (Demo-Parameter)</h2>
          </div>
          {params ? (
            <form
              className="stack"
              onSubmit={(e) => {
                e.preventDefault();
                void act(() => api.put("/settings/bot", { ...params, expected_mode: mode }), "Parameter gespeichert.");
              }}
            >
              <div className="form-grid">
                <label className="field">Symbol<input value={params.symbol} onChange={(e) => set("symbol", e.target.value)} disabled={running} /></label>
                <label className="field">Kurzer SMA (Kerzen)<input type="number" min={2} value={params.short_window} onChange={(e) => set("short_window", e.target.value)} disabled={running} /></label>
                <label className="field">Langer SMA (Kerzen)<input type="number" min={3} value={params.long_window} onChange={(e) => set("long_window", e.target.value)} disabled={running} /></label>
                <label className="field">Zielwert je Kauf<input type="number" step="0.01" min={1} value={params.order_value} onChange={(e) => set("order_value", e.target.value)} disabled={running} /></label>
                <label className="field">Max. Kerzenalter (s)<input type="number" min={60} value={params.max_bar_age_s} onChange={(e) => set("max_bar_age_s", e.target.value)} disabled={running} /></label>
              </div>
              <div className="row">
                <button className="btn" type="submit" disabled={busy || running}>Speichern</button>
                {running && <span className="small muted">Änderungen nur bei pausiertem Bot.</span>}
              </div>
              <div className="small muted">{info.data?.disclaimer}</div>
            </form>
          ) : (
            <div className="muted">Lade…</div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Entscheidungsverlauf</h2>
          <span className="small muted">Jede abgeschlossene Minutenkerze wird höchstens einmal verarbeitet.</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Kerze (Beginn)</th>
                <th>Symbol</th>
                <th className="num">Schluss</th>
                <th className="num">SMA kurz</th>
                <th className="num">SMA lang</th>
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
                    <td className="small">{dateTime(d.bar_time)}</td>
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
