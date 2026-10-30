import { useState } from "react";
import { api, errorText } from "../api";
import { StreamLine } from "../components/StreamBadge";
import { Card, EmptyState, Icon, Notice, Spinner, Tag } from "../components/ui";
import { dateTime, localTz, money, shortDT } from "../format";
import { usePoll } from "../hooks";
import type { ConnectionTest, EventRow, Mode, StreamsInfo } from "../types";

interface Diag {
  app_version: string;
  python: string;
  platform: string;
  mode: Mode;
  started_at: string;
  auth: Record<string, string | boolean | null>;
  env_files: string[];
  endpoints: { trading: string; market_data: string; feed: string; crypto_stream: string; order_stream: string };
  streams: StreamsInfo;
  binding: { host: string; port: number; loopback_only: boolean };
  db_path: string;
  runtimes: Record<string, { connection: string; last_ok_at: string | null; last_error: { kind: string; message: string; hint?: string } | null }>;
  single_process: string;
}

const AUTH_LABELS: Record<string, string> = {
  auth_mode: "ALPACA_AUTH_MODE",
  https_proxy_configured: "HTTPS-Proxy vorhanden",
  ca_bundle_configured: "CA-Bundle (SSL_CERT_FILE)",
  api_key_present: "ALPACA_API_KEY gesetzt",
  secret_key_present: "ALPACA_SECRET_KEY gesetzt",
};
const yn = (v: string | boolean | null) => (v === true ? "ja" : v === false ? "nein" : v ?? "nicht gesetzt");
const CONN: Record<string, [string, string]> = { ok: ["verbunden", "ok"], disconnected: ["getrennt", "warn"], error: ["Fehler", "bad"], unknown: ["unbekannt", ""] };

export function DiagnosticsPage({ mode }: { mode: Mode }) {
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
      <div className="grid-2">
        <Card title="Paper-Verbindungstest" actions={<button className="btn primary" onClick={runTest} disabled={busy}>{busy ? <Spinner /> : <Icon name="refresh" size={16} />} Verbindung testen</button>}>
          <div className="stack">
            <p className="sm muted">Lesender Aufruf <code>GET /v2/account</code> an paper-api.alpaca.markets – unabhängig vom aktiven Modus. Angezeigt werden nur Status, Währung, Cash und Kontowert.</p>
            {testErr && <Notice tone="error">{testErr}</Notice>}
            {test?.ok && (
              <Notice tone="ok" title={`Verbindung erfolgreich · ${dateTime(test.checked_at)}`}>
                Kontostatus {test.account_status} · {test.currency} · Cash {money(test.cash, test.currency)} · Kontowert {money(test.equity, test.currency)} · Auth-Modus {test.auth_mode}
              </Notice>
            )}
            {test && !test.ok && test.error && (
              <Notice tone="error" title={`${test.error.label ?? test.error.kind}: Verbindung nicht hergestellt`}>
                {test.error.message}
                {test.error.hint && <div className="small">Nächster Schritt: {test.error.hint}</div>}
                <div className="small muted">Geprüft {dateTime(test.checked_at)} · Auth-Modus {test.auth_mode ?? "nicht gesetzt"}</div>
              </Notice>
            )}
            {!test && !testErr && <EmptyState icon="diagnostics" title="Noch nicht getestet">„Verbindung testen“ prüft die Paper-Verbindung ohne Order.</EmptyState>}
          </div>
        </Card>

        <Card title="Diagnose" hint="ohne Zugangsdaten">
          {!d ? <div className="skeleton" style={{ height: 260 }} /> : (
            <dl className="kv">
              <dt>Version</dt><dd>{d.app_version} · Python {d.python} · {d.platform}</dd>
              <dt>Gestartet</dt><dd>{dateTime(d.started_at)}</dd>
              <dt>Aktiver Modus</dt><dd>{d.mode}</dd>
              {Object.entries(d.auth).map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}><dt>{AUTH_LABELS[k] ?? k}</dt><dd>{yn(v)}</dd></div>
              ))}
              <dt>Gelesene .env</dt><dd>{d.env_files.map((f) => <code key={f}>{f}</code>)}</dd>
              <dt>Handel</dt><dd>{d.endpoints.trading} (fest)</dd>
              <dt>Marktdaten</dt><dd>{d.endpoints.market_data} · Aktien-Feed {d.endpoints.feed}</dd>
              <dt>Datenströme</dt><dd>{d.endpoints.crypto_stream}<br />{d.endpoints.order_stream} (fest)<div className="muted small">{d.streams.note}</div></dd>
              <StreamLine label="Kursstrom" s={d.streams.market} />
              <StreamLine label="Order-Strom" s={d.streams.orders} />
              <dt>Bindung</dt><dd>{d.binding.host}:{d.binding.port} {d.binding.loopback_only ? <Tag tone="ok">nur localhost</Tag> : <Tag tone="bad">nicht nur localhost</Tag>}</dd>
              <dt>Datenbank</dt><dd><code>{d.db_path}</code></dd>
              {Object.entries(d.runtimes).map(([m, rt]) => {
                const [label, tn] = CONN[rt.connection] ?? [rt.connection, ""];
                return (
                  <div key={m} style={{ display: "contents" }}>
                    <dt>{m}</dt>
                    <dd><Tag tone={tn}>{label}</Tag> letzter Erfolg {dateTime(rt.last_ok_at)}{rt.last_error ? ` · ${rt.last_error.kind}: ${rt.last_error.message}` : ""}</dd>
                  </div>
                );
              })}
              <dt>Prozess</dt><dd>{d.single_process}</dd>
            </dl>
          )}
        </Card>
      </div>

      <Card title={`Ereignisprotokoll (${mode})`} bodyClass={null}>
        {!events.data?.events.length ? <EmptyState title="Keine Ereignisse" /> : (
          <div className="table-wrap" style={{ maxHeight: 520 }}>
            <table className="data">
              <thead><tr><th>Zeit ({localTz})</th><th>Bereich</th><th>Meldung</th></tr></thead>
              <tbody>
                {events.data.events.map((e) => (
                  <tr key={e.id}>
                    <td className="muted">{shortDT(e.ts)}</td>
                    <td><Tag tone={e.level === "error" ? "bad" : e.level === "warning" ? "warn" : ""}>{e.category}</Tag></td>
                    <td className="wrap">{e.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <p className="footer">
        Charts: TradingView Lightweight Charts™ · Copyright © TradingView, Inc. · <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">tradingview.com</a>
      </p>
    </>
  );
}
