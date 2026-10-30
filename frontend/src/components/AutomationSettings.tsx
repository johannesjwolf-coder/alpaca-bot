import { useEffect, useState } from "react";
import { api } from "../api";
import { dateTime } from "../format";
import type { NotifyInfo, NotifySettings, ResumeInfo } from "../types";
import { Card, Notice, Spinner, Tag, useConfirm } from "./ui";

type Act = (fn: () => Promise<unknown>, ok: string) => Promise<void>;

const NOTIFY_KINDS: [keyof NotifySettings, string, string][] = [
  ["trades", "Trades", "Einstiege und Ausstiege mit Ergebnis"],
  ["warnings", "Warnungen und Fehler", "Risikosperren, abgelehnte Orders, Verbindungsprobleme, Not-Aus"],
  ["bot", "Automatik", "Bot gestartet/pausiert, Neustart der Anwendung"],
  ["daily_summary", "Tageszusammenfassung", "Trades, Ergebnis und Kontowert einmal täglich"],
];

export function NotifyCard({ info, busy, act }: { info: NotifyInfo | undefined; busy: boolean; act: Act }) {
  const [form, setForm] = useState<NotifySettings | null>(null);
  useEffect(() => {
    if (info && !form) setForm(info.settings);
  }, [info, form]);
  const configured = info?.configured ?? false;
  return (
    <Card title="Benachrichtigungen (ntfy)"
          actions={info ? <Tag tone={configured ? "ok" : ""}>{configured ? `aktiv · ${info.server}` : "nicht eingerichtet"}</Tag> : undefined}>
      {!info || !form ? <div className="skeleton" style={{ height: 160 }} /> : (
        <div className="stack">
          {!configured && (
            <Notice tone={info.config_error ? "error" : ""}
                    title={info.config_error ? "ntfy-Konfiguration ungültig" : "So richten Sie Push-Nachrichten ein"}>
              {info.config_error ?? (
                <>
                  App „ntfy“ auf dem Handy installieren, ein schwer erratbares Thema abonnieren und dasselbe Thema auf dem Server in
                  der Datei <code>.env</code> als <code>NTFY_TOPIC</code> eintragen (Anleitung: docs/SERVER.md). Das Thema wird bewusst
                  nicht in der Oberfläche eingegeben.
                </>
              )}
            </Notice>
          )}
          <label className="check">
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
            <span><strong>Benachrichtigungen senden</strong></span>
          </label>
          {NOTIFY_KINDS.map(([k, label, help]) => (
            <label className="check" key={k}>
              <input type="checkbox" checked={Boolean(form[k])} disabled={!form.enabled}
                     onChange={(e) => setForm({ ...form, [k]: e.target.checked })} />
              <span>{label} <span className="muted">– {help}</span></span>
            </label>
          ))}
          <div className="form-grid">
            <label className="field">
              <span>Zusammenfassung um</span>
              <input type="time" value={form.summary_time} disabled={!form.enabled || !form.daily_summary}
                     onChange={(e) => setForm({ ...form, summary_time: e.target.value })} />
              <span className="field-help">Zeitzone {info.timezone}</span>
            </label>
          </div>
          <label className="check">
            <input type="checkbox" checked={form.paper_only} disabled={!form.enabled}
                   onChange={(e) => setForm({ ...form, paper_only: e.target.checked })} />
            <span>Nur PAPER melden <span className="muted">– DEMO-Ereignisse bleiben stumm</span></span>
          </label>
          {configured && (
            <p className="field-help">
              Gesendet: {info.sent_total}{info.last_sent_at ? ` · zuletzt ${dateTime(info.last_sent_at)}` : ""} · Token: {info.token_set ? "gesetzt" : "keins"}
              {info.last_error && <> · <span className="neg">Letzter Fehler: {info.last_error}</span></>}
            </p>
          )}
          <div className="row">
            <button className="btn primary" disabled={busy}
                    onClick={() => act(() => api.put("/settings/notify", form), "Benachrichtigungen gespeichert.")}>
              Speichern
            </button>
            <button className="btn" disabled={busy || !configured} onClick={() => act(async () => {
              const r = await api.post<{ ok: boolean; error: string | null }>("/notify/test");
              if (!r.ok) throw new Error(r.error ?? "Testnachricht nicht zugestellt.");
            }, "Testnachricht gesendet – sie sollte gleich auf dem Handy erscheinen.")}>
              Testnachricht senden
            </button>
          </div>
          <p className="field-help">Nachrichten enthalten Symbole, Kurse und Ergebnisse, nie Zugangsdaten. Höchstens 12 Meldungen pro Minute.</p>
        </div>
      )}
    </Card>
  );
}

export function ResumeCard({ info, busy, act }: { info: ResumeInfo | undefined; busy: boolean; act: Act }) {
  const confirm = useConfirm();
  return (
    <Card title="Nach einem Neustart"
          actions={info ? <Tag tone={info.enabled ? "warn" : ""}>{info.enabled ? "Fortsetzen aktiv" : "Alles aus (Standard)"}</Tag> : undefined}>
      {!info ? <div className="skeleton" style={{ height: 120 }} /> : (
        <div className="stack">
          <p className="sm">
            Standard: Nach jedem Neustart (Update, Server-Neustart, Absturz) sind Automatik und Paper-Orderfreigabe aus, bis Sie sie
            wieder einschalten.
          </p>
          <p className="sm">
            Mit <strong>Fortsetzen nach Neustart</strong> stellt die Anwendung Modus, Paper-Orderfreigabe und Bot selbst wieder her –
            aber nur, wenn der Bot beim Neustart lief. Haben Sie ihn pausiert, die Freigabe aufgehoben oder Not-Aus ausgelöst, bleibt
            alles aus. Gelingt der Start in 10 Versuchen (ca. 5 Minuten) nicht, wird die Freigabe wieder aufgehoben.
          </p>
          <dl className="kv">
            <dt>Zuletzt</dt>
            <dd>
              {info.last_run.mode ? `${info.last_run.mode} · Bot ${info.last_run.bot ? "lief" : "pausiert"}` : "—"}
              {info.last_run.updated_at ? ` (${dateTime(info.last_run.updated_at)})` : ""}
            </dd>
            {info.status && <><dt>Letztes Fortsetzen</dt><dd>{info.pending && <Spinner />} {info.status}</dd></>}
          </dl>
          <div className="row">
            {info.enabled ? (
              <button className="btn" disabled={busy}
                      onClick={() => act(() => api.put("/settings/resume", { enabled: false }), "Fortsetzen nach Neustart ausgeschaltet.")}>
                Ausschalten
              </button>
            ) : (
              <button className="btn primary" disabled={busy} onClick={async () => {
                const ok = await confirm({
                  title: "Fortsetzen nach Neustart einschalten?",
                  confirmLabel: "Einschalten",
                  body: (
                    <p>
                      Nach einem Neustart sendet der Bot wieder <strong>automatisch Paper-Orders</strong>, ohne dass Sie die Freigabe erneut
                      bestätigen. Risikogrenzen, Tagesverlust-Sperre und Not-Aus gelten unverändert.
                    </p>
                  ),
                });
                if (ok) await act(() => api.put("/settings/resume", { enabled: true, confirm: true }), "Fortsetzen nach Neustart eingeschaltet.");
              }}>
                Fortsetzen einschalten…
              </button>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
