import { useEffect, useState } from "react";
import { api, errorText } from "../api";
import { NotifyCard, ResumeCard } from "../components/AutomationSettings";
import { Card, Notice, Seg, Spinner, Tag, useConfirm } from "../components/ui";
import { usePoll } from "../hooks";
import type { Mode, NotifyInfo, ResumeInfo, RiskLimits, Status } from "../types";

interface SettingsResponse {
  mode: Mode;
  risk_limits: RiskLimits;
  defaults: { risk_limits: RiskLimits };
  notify: NotifyInfo;
  resume: ResumeInfo;
  note: string;
}

const LIMITS: [keyof RiskLimits, string, string, string][] = [
  ["max_order_value", "Max. Orderwert", "0.01", "USD · Grösse je Trade"],
  ["max_total_invested", "Max. investierter Gesamtbetrag", "0.01", "USD"],
  ["max_open_positions", "Max. offene Positionen", "1", "Anzahl"],
  ["max_buys_per_hour", "Max. Einstiege pro Stunde", "1", "Käufe"],
  ["max_buys_per_day", "Max. Einstiege pro Tag", "1", "Käufe"],
  ["max_orders_per_day", "Max. Orders pro Tag", "1", "Käufe + Verkäufe"],
  ["max_daily_loss", "Max. Tagesverlust", "0.01", "USD, inkl. unrealisiert"],
  ["loss_streak_limit", "Pause nach Verlusten in Folge", "1", "Trades"],
  ["loss_pause_min", "Dauer der Kaufpause", "1", "Minuten"],
  ["max_spread_pct", "Max. Spread für Einstiege", "0.01", "% (Krypto)"],
  ["price_buffer_pct", "Kurspuffer Market-Order", "0.1", "%"],
];

export function SettingsPage({ status, onChange }: { status: Status; onChange: () => void }) {
  const mode = status.mode;
  const confirm = useConfirm();
  const settings = usePoll(() => api.get<SettingsResponse>("/settings"), 60_000, [mode]);
  const [limits, setLimits] = useState<RiskLimits | null>(null);
  const [loadedFor, setLoadedFor] = useState<Mode | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);

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
      setMsg({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
      onChange();
      void settings.reload();
    }
  }

  async function switchMode(target: Mode) {
    if (target === mode) return;
    const ok = await confirm({
      title: `In den ${target}-Modus wechseln?`,
      confirmLabel: `${target} aktivieren`,
      body: target === "PAPER"
        ? <p>Die Anwendung verbindet sich mit Ihrem <strong>Alpaca-Paper-Konto</strong> (Spielgeld). Der Bot wird pausiert, die Paper-Orderfreigabe bleibt aus.</p>
        : <p>DEMO nutzt simulierte Daten ohne Alpaca-Zugriff. Der Bot wird pausiert.</p>,
    });
    if (ok) await act(() => api.post("/mode", { mode: target }), `Modus ${target} aktiv.`);
  }

  const invalid = limits ? LIMITS.filter(([k]) => !(Number(limits[k]) > 0)).map(([k]) => k) : [];

  return (
    <>
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      <div className="grid-2">
        <div className="stack" style={{ gap: 16 }}>
          <Card title="Betriebsart">
            <div className="stack">
              <Seg label="Betriebsart" value={mode} onChange={switchMode} disabled={busy}
                   options={[{ id: "DEMO", label: "DEMO · simuliert" }, { id: "PAPER", label: "PAPER · Alpaca-Paper-Konto" }]} />
              <p className="field-help">
                <strong>DEMO</strong>: reproduzierbare Beispieldaten, simulierte Orders, kein Alpaca-Zugriff. <strong>PAPER</strong>: Ihr
                Alpaca-Paper-Konto über paper-api.alpaca.markets, Kurse über data.alpaca.markets. Ein Echtgeldmodus existiert nicht.
                Beide Modi speichern ihre Daten getrennt.
              </p>
            </div>
          </Card>

          <Card title="Paper-Orderfreigabe" actions={mode === "PAPER" ? <Tag tone={status.orders_enabled ? "warn" : ""}>{status.orders_enabled ? "Freigegeben" : "Gesperrt"}</Tag> : undefined}>
            {mode === "PAPER" ? (
              <div className="stack">
                <p className="sm">Ohne Freigabe sind manuelle und automatische Paper-Orders gesperrt. Nach jedem Neustart ist sie wieder aus{status.resume_after_restart ? " – ausser beim Fortsetzen einer laufenden Automatik (siehe unten)" : ""}.</p>
                <div className="row">
                  {status.orders_enabled ? (
                    <button className="btn" disabled={busy} onClick={() => act(() => api.post("/orders-release", { expected_mode: mode, enabled: false }), "Freigabe aufgehoben.")}>
                      Freigabe aufheben
                    </button>
                  ) : (
                    <button className="btn primary" disabled={busy} onClick={async () => {
                      const ok = await confirm({
                        title: "Paper-Orders freigeben?", confirmLabel: "Paper-Orders freigeben",
                        body: <p>Danach können manuelle und – nach ausdrücklichem Start – automatische Orders an Ihr <strong>Alpaca-Paper-Konto</strong> gesendet werden. Nach einem Neustart ist die Freigabe wieder aus.</p>,
                      });
                      if (ok) await act(() => api.post("/orders-release", { expected_mode: mode, enabled: true }), "Paper-Orders freigegeben.");
                    }}>
                      {busy ? <Spinner /> : null} Paper-Orders freigeben…
                    </button>
                  )}
                </div>
              </div>
            ) : (
              <p className="sm muted">Nur im PAPER-Modus relevant. DEMO-Orders werden lokal simuliert.</p>
            )}
          </Card>

          {mode === "DEMO" && (
            <Card title="DEMO-Konto">
              <div className="stack">
                <p className="sm">Setzt das simulierte Konto auf 10’000 USD zurück und löscht DEMO-Orders, -Trades und Entscheidungen.</p>
                <div className="row">
                  <button className="btn danger" disabled={busy} onClick={async () => {
                    const ok = await confirm({ title: "DEMO-Konto zurücksetzen?", tone: "sell", confirmLabel: "Zurücksetzen", body: <p>Nur DEMO-Daten sind betroffen. Das PAPER-Konto bleibt unverändert.</p> });
                    if (ok) await act(() => api.post("/demo/reset", { expected_mode: mode }), "DEMO-Konto zurückgesetzt.");
                  }}>DEMO zurücksetzen…</button>
                </div>
              </div>
            </Card>
          )}
        </div>

        <Card title={`Risikogrenzen (${mode})`} actions={<Tag tone="gold">Technische Paper-Testwerte</Tag>}>
          {!limits ? <div className="skeleton" style={{ height: 320 }} /> : (
            <form className="stack" noValidate onSubmit={(e) => {
              e.preventDefault();
              if (invalid.length) return;
              void act(() => api.put("/settings/risk", { ...limits, expected_mode: mode }), "Risikogrenzen gespeichert.");
            }}>
              <div className="form-grid">
                {LIMITS.map(([k, label, step, unit]) => (
                  <label className={`field ${invalid.includes(k) ? "invalid" : ""}`} key={k}>
                    <span>{label}</span>
                    <input type="number" step={step} min={0} value={limits[k]} onChange={(e) => setLimits({ ...limits, [k]: Number(e.target.value) })} />
                    {invalid.includes(k) ? <span className="field-error">Wert muss grösser als 0 sein.</span> : <span className="field-help">{unit}</span>}
                  </label>
                ))}
              </div>
              <p className="field-help">
                {settings.data?.note} Die Grenzen gelten serverseitig für alle Orderwege (manuell und Bot). Tagesverlust = Kontowert jetzt −
                Kontowert zum letzten Handelsschluss; bei Verletzung sind neue Käufe bis zum nächsten Handelstag (00:00 New York) gesperrt,
                Verkäufe bleiben möglich.
              </p>
              <div className="row">
                <button className="btn primary" type="submit" disabled={busy || invalid.length > 0}>Risikogrenzen speichern</button>
                <button className="btn" type="button" disabled={!settings.data} onClick={() => settings.data && setLimits(settings.data.defaults.risk_limits)}>
                  Standardwerte einsetzen
                </button>
              </div>
            </form>
          )}
        </Card>
      </div>
      <div className="grid-2">
        <NotifyCard info={settings.data?.notify} busy={busy} act={act} />
        <ResumeCard info={settings.data?.resume} busy={busy} act={act} />
      </div>
    </>
  );
}
