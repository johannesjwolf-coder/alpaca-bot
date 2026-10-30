import { useState } from "react";
import { api, errorText } from "../api";
import { dateTime, money } from "../format";
import { usePoll } from "../hooks";
import type { ConnectionTest, EventRow, Mode } from "../types";

interface Diag {
  app_version: string;
  python: string;
  platform: string;
  mode: Mode;
  started_at: string;
  auth: Record<string, string | boolean | null>;
  endpoints: { trading: string; market_data: string; feed: string };
  binding: { host: string; port: number; loopback_only: boolean };
  allowed_origins: string[];
  db_path: string;
  runtimes: Record<string, { connection: string; last_ok_at: string | null; last_error: { kind: string; message: string; hint?: string } | null }>;
  single_process: string;
}

const AUTH_LABELS: Record<string, string> = {
  auth_mode: "ALPACA_AUTH_MODE",
  https_proxy_configured: "HTTPS-Proxy vorhanden",
  ca_bundle_configured: "CA-Bundle (SSL_CERT_FILE) gesetzt",
  api_key_present: "ALPACA_API_KEY gesetzt",
  secret_key_present: "ALPACA_SECRET_KEY gesetzt",
};

function yn(v: string | boolean | null): string {
  if (v === true) return "ja";
  if (v === false) return "nein";
  return v ?? "nicht gesetzt";
}

export function Diagnostics({ mode }: { mode: Mode }) {
  const diag = usePoll(() => api.get<Diag>("/diagnostics"), 15_000, [mode]);
  const events = usePoll(() => api.get<{ events: EventRow[] }>("/events?limit=80"), 5000, [mode]);
  const [test, setTest] = useState<ConnectionTest | null>(null);
  const [testErr, setTestErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function runTest() {
    setBusy(true);
    setTestErr(null);
    try {
      setTest(await api.get<ConnectionTest>("/connection-test"));
    } catch (e) {
      setTest(null);
      setTestErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const d = diag.data;
  return (
    <>
      <div className="grid cols-2">
        <div className="card stack">
          <div className="card-head">
            <h2>Paper-Verbindungstest</h2>
            <button className="btn" onClick={runTest} disabled={busy}>{busy ? "Prüfe…" : "Verbindung testen"}</button>
          </div>
          <div className="small muted">
            Lesender Aufruf <code>GET /v2/account</code> an paper-api.alpaca.markets – unabhängig vom aktiven Modus.
            Angezeigt werden nur Status, Währung, Cash und Kontowert.
          </div>
          {testErr && <div className="notice bad">{testErr}</div>}
          {test && test.ok && (
            <div className="notice ok">
              <strong>Verbindung erfolgreich ({dateTime(test.checked_at)})</strong>
              Kontostatus {test.account_status} · Währung {test.currency} · Cash {money(test.cash, test.currency)} ·
              Kontowert {money(test.equity, test.currency)} · Auth-Modus {test.auth_mode}
            </div>
          )}
          {test && !test.ok && test.error && (
            <div className="notice bad">
              <strong>{test.error.label ?? test.error.kind}: Verbindung NICHT hergestellt</strong>
              {test.error.message}
              {test.error.hint && <div className="small">Nächster Schritt: {test.error.hint}</div>}
              <div className="small muted">Geprüft {dateTime(test.checked_at)} · Auth-Modus {test.auth_mode ?? "nicht gesetzt"}</div>
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head"><h2>Diagnose (ohne Zugangsdaten)</h2></div>
          {d ? (
            <dl className="kv">
              <dt>Version</dt><dd>{d.app_version} · Python {d.python} · {d.platform}</dd>
              <dt>Gestartet</dt><dd>{dateTime(d.started_at)}</dd>
              <dt>Aktiver Modus</dt><dd>{d.mode}</dd>
              {Object.entries(d.auth).map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}><dt>{AUTH_LABELS[k] ?? k}</dt><dd>{yn(v)}</dd></div>
              ))}
              <dt>Handel</dt><dd>{d.endpoints.trading} (fest)</dd>
              <dt>Marktdaten</dt><dd>{d.endpoints.market_data} · Feed {d.endpoints.feed}</dd>
              <dt>Bindung</dt>
              <dd>{d.binding.host}:{d.binding.port} {d.binding.loopback_only ? "(nur localhost)" : "(NICHT nur localhost!)"}</dd>
              <dt>Datenbank</dt><dd><code>{d.db_path}</code></dd>
              {Object.entries(d.runtimes).map(([m, rt]) => (
                <div key={m} style={{ display: "contents" }}>
                  <dt>{m}</dt>
                  <dd>
                    {rt.connection} · letzter Erfolg {dateTime(rt.last_ok_at)}
                    {rt.last_error ? ` · Fehler: ${rt.last_error.kind} – ${rt.last_error.message}` : ""}
                  </dd>
                </div>
              ))}
              <dt>Prozess</dt><dd>{d.single_process}</dd>
            </dl>
          ) : (
            <div className="muted">{diag.error ?? "Lade…"}</div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>Ereignisprotokoll ({mode})</h2></div>
        <ul className="events">
          {events.data?.events.length === 0 && <li className="empty">Keine Ereignisse</li>}
          {events.data?.events.map((e) => (
            <li key={e.id}>
              <span className="small muted">{dateTime(e.ts)}</span>
              <span className={`small tone-${e.level === "error" ? "bad" : e.level === "warning" ? "warn" : "muted"}`}>
                {e.category}
              </span>
              <span>{e.message}</span>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}
