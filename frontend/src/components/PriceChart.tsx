import { useMemo, useState } from "react";
import { api } from "../api";
import { ago, dateTime, localTz, num, timeOnly } from "../format";
import { usePoll } from "../hooks";
import type { BarsResponse, Mode } from "../types";

const W = 900;
const H = 300;
const PAD = { l: 56, r: 14, t: 12, b: 26 };

/** Gleitender Durchschnitt ab Index ``start`` (wie der Bot: nur Kerzen der laufenden Sitzung). */
function smaSeries(closes: number[], n: number, start = 0): (number | null)[] {
  let sum = 0;
  return closes.map((c, i) => {
    if (i < start) return null;
    sum += c;
    if (i - start >= n) sum -= closes[i - n];
    return i - start >= n - 1 ? sum / n : null;
  });
}

function path(values: (number | null)[], x: (i: number) => number, y: (v: number) => number): string {
  let d = "";
  let pen = false;
  values.forEach((v, i) => {
    if (v === null) {
      pen = false;
      return;
    }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}

const STATUS_TONE: Record<BarsResponse["status"], string> = {
  ok: "ok",
  stale: "warn",
  market_closed: "muted",
  empty: "warn",
  error: "bad",
};
const STATUS_LABEL: Record<BarsResponse["status"], string> = {
  ok: "aktuell",
  stale: "veraltet",
  market_closed: "Markt geschlossen",
  empty: "keine Daten",
  error: "Fehler",
};

export function PriceChart({ mode, defaultSymbol }: { mode: Mode; defaultSymbol: string }) {
  const [input, setInput] = useState(defaultSymbol);
  const [symbol, setSymbol] = useState(defaultSymbol);
  const bars = usePoll(
    () => api.get<BarsResponse>(`/bars?symbol=${encodeURIComponent(symbol)}`),
    20_000,
    [symbol, mode],
  );
  const res = bars.data;

  const chart = useMemo(() => {
    if (!res || res.bars.length < 2) return null;
    const closes = res.bars.map((b) => b.c);
    const shortN = res.strategy?.short_window;
    const longN = res.strategy?.long_window;
    // Sitzungsfilter (PAPER): Durchschnitte erst ab Eröffnung der laufenden Sitzung.
    const sessionOpen = res.strategy?.session_open ? new Date(res.strategy.session_open).getTime() : null;
    let start = 0;
    if (res.strategy?.session_filter) {
      start = sessionOpen === null ? closes.length : res.bars.findIndex((b) => new Date(b.t).getTime() >= sessionOpen);
      if (start < 0) start = closes.length;
    }
    const s = shortN ? smaSeries(closes, shortN, start) : [];
    const l = longN ? smaSeries(closes, longN, start) : [];
    const all = [...res.bars.map((b) => b.l), ...res.bars.map((b) => b.h)];
    let min = Math.min(...all);
    let max = Math.max(...all);
    const pad = (max - min) * 0.08 || max * 0.001 || 1;
    min -= pad;
    max += pad;
    const n = res.bars.length;
    const x = (i: number) => PAD.l + (i / (n - 1)) * (W - PAD.l - PAD.r);
    const y = (v: number) => PAD.t + (1 - (v - min) / (max - min)) * (H - PAD.t - PAD.b);
    const yTicks = Array.from({ length: 5 }, (_, i) => min + ((max - min) * i) / 4);
    const xTicks = Array.from({ length: 6 }, (_, i) => Math.round(((n - 1) * i) / 5));
    return {
      close: path(closes, x, y),
      short: s.length ? path(s, x, y) : "",
      long: l.length ? path(l, x, y) : "",
      yTicks: yTicks.map((v) => ({ y: y(v), label: num(v, 2) })),
      xTicks: xTicks.map((i) => ({ x: x(i), label: timeOnly(res.bars[i].t) })),
      shortN,
      longN,
      sessionStartX: res.strategy?.session_filter && start > 0 && start < n ? x(start) : null,
    };
  }, [res]);

  return (
    <div className="card">
      <div className="card-head">
        <div className="row">
          <h2>Kursverlauf</h2>
          {res && (
            <span className={`badge ${STATUS_TONE[res.status]}`} title={res.message}>
              {STATUS_LABEL[res.status]}
            </span>
          )}
          {mode === "DEMO" && <span className="badge mode-DEMO">DEMO-Daten</span>}
        </div>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            const s = input.trim().toUpperCase();
            if (s) setSymbol(s);
          }}
        >
          <input
            aria-label="Symbol"
            value={input}
            onChange={(e) => setInput(e.target.value.toUpperCase())}
            style={{ width: 110 }}
            maxLength={8}
          />
          <button className="btn" type="submit">
            Anzeigen
          </button>
        </form>
      </div>

      {bars.loading && !res && <div className="chart-placeholder">Lade Minutenkerzen für {symbol}…</div>}
      {bars.error && !res && (
        <div className="chart-placeholder tone-bad">Kursdaten konnten nicht geladen werden: {bars.error}</div>
      )}
      {res && res.status === "error" && (
        <div className="chart-placeholder tone-bad">
          {res.message}
          {res.error?.hint ? <div className="small muted">{res.error.hint}</div> : null}
        </div>
      )}
      {res && res.status !== "error" && !chart && (
        <div className="chart-placeholder">{res.message || "Keine Daten"}</div>
      )}
      {res && chart && (
        <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img"
             aria-label={`Minutenkerzen ${res.symbol}`}>
          {chart.yTicks.map((t) => (
            <g key={t.y}>
              <line className="grid-line" x1={PAD.l} x2={W - PAD.r} y1={t.y} y2={t.y} />
              <text className="axis-label" x={PAD.l - 8} y={t.y + 4} textAnchor="end">
                {t.label}
              </text>
            </g>
          ))}
          {chart.xTicks.map((t) => (
            <text key={t.x} className="axis-label" x={t.x} y={H - 6} textAnchor="middle">
              {t.label}
            </text>
          ))}
          {chart.sessionStartX !== null && (
            <g>
              <line x1={chart.sessionStartX} x2={chart.sessionStartX} y1={PAD.t} y2={H - PAD.b}
                    stroke="var(--faint)" strokeDasharray="4 4" />
              <text className="axis-label" x={chart.sessionStartX + 4} y={PAD.t + 12}>Sitzungsbeginn</text>
            </g>
          )}
          <path d={chart.close} fill="none" stroke="var(--text)" strokeWidth={1.4} vectorEffect="non-scaling-stroke" />
          {chart.long && (
            <path d={chart.long} fill="none" stroke="var(--paper)" strokeWidth={1.6} vectorEffect="non-scaling-stroke" />
          )}
          {chart.short && (
            <path d={chart.short} fill="none" stroke="var(--accent)" strokeWidth={1.6} vectorEffect="non-scaling-stroke" />
          )}
        </svg>
      )}

      {res && (
        <div className="stack" style={{ marginTop: 10 }}>
          <div className="legend">
            <span><i style={{ background: "var(--text)" }} />Schlusskurs {res.symbol} (1 Min.)</span>
            {chart?.shortN && <span><i style={{ background: "var(--accent)" }} />SMA {chart.shortN}</span>}
            {chart?.longN && <span><i style={{ background: "var(--paper)" }} />SMA {chart.longN}</span>}
            <span>Achse: {localTz}</span>
            {res.strategy?.session_filter && (
              <span>SMA nur aus Kerzen der laufenden Sitzung (wie der Bot)</span>
            )}
          </div>
          <div className="small muted">
            Quelle: {res.source}. Letzte Kerze: {dateTime(res.last_bar_at)} · abgerufen {ago(res.fetched_at)}
            {res.cached ? " (Cache)" : ""}
          </div>
          {res.status !== "ok" && res.status !== "error" && <div className="small tone-warn">{res.message}</div>}
        </div>
      )}
    </div>
  );
}
