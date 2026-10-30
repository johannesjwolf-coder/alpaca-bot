import { useEffect, useState } from "react";
import { api, errorText } from "../api";
import { usePoll } from "../hooks";
import type { Mode, RiskLimits, Status } from "../types";

interface SettingsResponse {
  mode: Mode;
  risk_limits: RiskLimits;
  defaults: { risk_limits: RiskLimits };
  note: string;
}

const LIMIT_FIELDS: [keyof RiskLimits, string, string][] = [
  ["max_order_value", "Max. Orderwert", "0.01"],
  ["max_total_invested", "Max. investierter Gesamtbetrag", "0.01"],
  ["max_open_positions", "Max. offene Positionen", "1"],
  ["max_orders_per_day", "Max. Orders pro Tag", "1"],
  ["max_daily_loss", "Max. Tagesverlust (inkl. unrealisiert)", "0.01"],
  ["price_buffer_pct", "Kurspuffer Market-Order (%)", "0.1"],
];

export function SettingsPanel({ status, onChange }: { status: Status; onChange: () => void }) {
  const mode = status.mode;
  const settings = usePoll(() => api.get<SettingsResponse>("/settings"), 60_000, [mode]);
  const [limits, setLimits] = useState<RiskLimits | null>(null);
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // Formular nur beim ersten Laden bzw. Moduswechsel befüllen, damit Polling keine Eingaben überschreibt.
  const [loadedFor, setLoadedFor] = useState<Mode | null>(null);
  useEffect(() => {
    if (settings.data && settings.data.mode !== loadedFor) {
      setLimits(settings.data.risk_limits);
      setLoadedFor(settings.data.mode);
    }
  }, [settings.data, loadedFor]);

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
      void settings.reload();
    }
  }

  function switchMode(target: Mode) {
    if (target === mode) return;
    const text =
      target === "PAPER"
        ? "In den PAPER-Modus wechseln?\nDie Anwendung verbindet sich mit Ihrem Alpaca-Paper-Konto. Der Bot wird pausiert, die Orderfreigabe bleibt aus."
        : "In den DEMO-Modus wechseln? Der Bot wird pausiert.";
    if (!window.confirm(text)) return;
    void act(() => api.post("/mode", { mode: target }), `Modus ${target} aktiv.`);
  }

  return (
    <div className="grid cols-2">
      <div className="card stack">
        <div className="card-head"><h2>Betriebsart</h2></div>
        <div className="segmented" role="radiogroup" aria-label="Betriebsart">
          {(["DEMO", "PAPER"] as Mode[]).map((m) => (
            <button key={m} className={mode === m ? "active" : ""} onClick={() => switchMode(m)} disabled={busy}>
              {m}
            </button>
          ))}
        </div>
        <div className="small muted">
          <strong>DEMO</strong>: lokale, reproduzierbare Beispieldaten und simulierte Orders, kein Alpaca-Zugriff.{" "}
          <strong>PAPER</strong>: Ihr Alpaca-Paper-Konto (Spielgeld) über paper-api.alpaca.markets, Marktdaten über
          data.alpaca.markets (IEX). Ein Echtgeldmodus existiert nicht. Beide Modi speichern ihre Daten getrennt.
        </div>

        <div className="card-head" style={{ marginTop: 10 }}><h2>Paper-Orderfreigabe</h2></div>
        {mode === "PAPER" ? (
          <>
            <div className="row">
              <span className={`badge ${status.orders_enabled ? "warn" : "muted"}`}>
                {status.orders_enabled ? "Freigegeben" : "Gesperrt"}
              </span>
              {status.orders_enabled ? (
                <button className="btn" disabled={busy}
                        onClick={() => void act(() => api.post("/orders-release", { expected_mode: mode, enabled: false }), "Freigabe deaktiviert.")}>
                  Freigabe aufheben
                </button>
              ) : (
                <button className="btn danger" disabled={busy}
                        onClick={() => {
                          if (!window.confirm("Paper-Orders freigeben?\nDanach können manuelle und (nach Start) automatische Orders an Ihr Alpaca-Paper-Konto gesendet werden. Nach einem Neustart ist die Freigabe wieder aus.")) return;
                          void act(() => api.post("/orders-release", { expected_mode: mode, enabled: true }), "Paper-Orders freigegeben.");
                        }}>
                  Paper-Orders freigeben…
                </button>
              )}
            </div>
            <div className="small muted">Ohne Freigabe sind manuelle und automatische Paper-Aufträge gesperrt. Standard: aus.</div>
          </>
        ) : (
          <div className="small muted">Nur im PAPER-Modus relevant. DEMO-Orders werden lokal simuliert.</div>
        )}

        {mode === "DEMO" && (
          <>
            <div className="card-head" style={{ marginTop: 10 }}><h2>DEMO-Konto</h2></div>
            <div className="row">
              <button className="btn" disabled={busy}
                      onClick={() => {
                        if (!window.confirm("DEMO-Konto zurücksetzen (10’000 USD, keine Positionen, keine Demo-Orders)?")) return;
                        void act(() => api.post("/demo/reset", { expected_mode: mode }), "DEMO-Konto zurückgesetzt.");
                      }}>
                DEMO zurücksetzen…
              </button>
            </div>
          </>
        )}
        {msg && <div className={`notice ${msg.tone}`}>{msg.text}</div>}
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Risikogrenzen ({mode})</h2>
          <span className="badge warn">Technische Paper-Testwerte</span>
        </div>
        {limits ? (
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              void act(() => api.put("/settings/risk", { ...limits, expected_mode: mode }), "Risikogrenzen gespeichert.");
            }}
          >
            <div className="form-grid">
              {LIMIT_FIELDS.map(([k, label, step]) => (
                <label className="field" key={k}>
                  {label}
                  <input type="number" step={step} min={0} value={limits[k]}
                         onChange={(e) => setLimits({ ...limits, [k]: Number(e.target.value) })} />
                </label>
              ))}
            </div>
            <div className="small muted">
              {settings.data?.note} Die Grenzen gelten serverseitig für alle Orderwege (manuell und Bot). Beträge in
              Kontowährung. Tagesverlust = Kontowert jetzt − Kontowert zum letzten Handelsschluss; bei Verletzung sind
              neue Käufe bis zum nächsten Handelstag (00:00 New York) gesperrt, Verkäufe bleiben möglich.
            </div>
            <div className="row">
              <button className="btn" type="submit" disabled={busy}>Speichern</button>
              <button className="btn" type="button" disabled={busy || !settings.data}
                      onClick={() => settings.data && setLimits(settings.data.defaults.risk_limits)}>
                Standardwerte einsetzen
              </button>
            </div>
          </form>
        ) : (
          <div className="muted">{settings.error ?? "Lade…"}</div>
        )}
      </div>
    </div>
  );
}
