import { useMemo, useState } from "react";
import { api } from "../../api";
import { ago, dateTime, localTz, money, num } from "../../format";
import { usePoll } from "../../hooks";
import type { BarsResponse, Mode, OrderRow, SymbolSuggestion, Timeframe } from "../../types";
import { QuickSymbols, SymbolPicker } from "../SymbolPicker";
import { EmptyState, Icon, Seg, Skeleton, Tag } from "../ui";
import { CHART } from "./theme";
import { TradingChart, type ChartMarker, type MaSpec } from "./TradingChart";

const TF: { id: Timeframe; label: string; sec: number }[] = [
  { id: "1Min", label: "1m", sec: 60 },
  { id: "5Min", label: "5m", sec: 300 },
  { id: "15Min", label: "15m", sec: 900 },
  { id: "1Hour", label: "1h", sec: 3600 },
  { id: "1Day", label: "1T", sec: 86400 },
];
const POLL: Record<Timeframe, number> = { "1Min": 20_000, "5Min": 30_000, "15Min": 60_000, "1Hour": 120_000, "1Day": 300_000 };

const STATUS: Record<BarsResponse["status"], [string, string]> = {
  ok: ["Aktuell", "ok"],
  stale: ["Veraltet", "warn"],
  market_closed: ["Markt geschlossen", "info"],
  empty: ["Keine Daten", "warn"],
  error: ["Fehler", "bad"],
};

function readPref<T extends string>(key: string, fallback: T, allowed: readonly T[]): T {
  try {
    const v = localStorage.getItem(key) as T | null;
    return v && allowed.includes(v) ? v : fallback;
  } catch {
    return fallback;
  }
}
function writePref(key: string, v: string) {
  try {
    localStorage.setItem(key, v);
  } catch {
    /* Speicher nicht verfügbar – nur Komfort */
  }
}

export function ordersToMarkers(orders: OrderRow[], symbol: string, currency: string | null): ChartMarker[] {
  return orders
    .filter((o) => o.symbol === symbol && o.filled_qty > 0 && o.filled_avg_price)
    .map((o) => ({
      time: o.filled_at ?? o.updated_at ?? o.created_at ?? "",
      side: o.side,
      label: `${o.side === "buy" ? "Kauf" : "Verkauf"} ${num(o.filled_qty, 8)} zu ${money(o.filled_avg_price, currency)} · ${
        o.source === "bot" ? "Bot" : o.source === "manual" ? "manuell" : "extern"
      }`,
    }))
    .filter((m) => m.time);
}

export function ChartPanel({ mode, symbol, onSymbol, suggestions, markers, strategyMa }: {
  mode: Mode;
  symbol: string;
  onSymbol: (s: string) => void;
  suggestions: SymbolSuggestion[];
  markers: ChartMarker[];
  strategyMa?: { short: number; long: number } | null;
}) {
  const [tf, setTf] = useState<Timeframe>(() => readPref("chart.tf", "1Min", TF.map((t) => t.id)));
  const [type, setType] = useState<"candles" | "line">(() => readPref("chart.type", "candles", ["candles", "line"] as const));
  const [showVol, setShowVol] = useState(() => readPref("chart.vol", "1", ["1", "0"] as const) === "1");
  const [showMa, setShowMa] = useState(() => readPref("chart.ma", "1", ["1", "0"] as const) === "1");
  const [fit, setFit] = useState(0);
  const tfSec = TF.find((t) => t.id === tf)!.sec;
  const res = usePoll(
    () => api.get<BarsResponse>(`/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${tf}&limit=400`),
    POLL[tf],
    [symbol, tf, mode],
  );
  const data = res.data && res.data.symbol === symbol && res.data.timeframe === tf ? res.data : null;

  const mas: MaSpec[] = useMemo(() => {
    if (!showMa) return [];
    const strat = data?.strategy;
    if (strat && tf === "1Min") {
      const sessionStart = strat.session_open && data
        ? Math.max(0, data.bars.findIndex((b) => new Date(b.t).getTime() >= new Date(strat.session_open!).getTime()))
        : 0;
      return [
        { period: strat.short_window, color: CHART.maGold, label: `SMA ${strat.short_window} (Bot)`, startIndex: sessionStart },
        { period: strat.long_window, color: CHART.maSlate, label: `SMA ${strat.long_window} (Bot)`, startIndex: sessionStart },
      ];
    }
    if (strategyMa) {
      return [
        { period: strategyMa.short, color: CHART.maGold, label: `SMA ${strategyMa.short}` },
        { period: strategyMa.long, color: CHART.maSlate, label: `SMA ${strategyMa.long}` },
      ];
    }
    return [
      { period: 20, color: CHART.maGold, label: "SMA 20" },
      { period: 50, color: CHART.maSlate, label: "SMA 50" },
    ];
  }, [showMa, data, tf, strategyMa]);

  const status = data ? STATUS[data.status] : null;
  const hasVolume = !!data?.bars.some((b) => b.v > 0);

  return (
    <section className="card chart-card" aria-label={`Chart ${symbol}`}>
      <div className="chart-toolbar">
        <SymbolPicker value={symbol} onChange={onSymbol} mode={mode} />
        <Seg label="Zeitintervall" value={tf} onChange={(v) => { setTf(v); writePref("chart.tf", v); }}
             options={TF.map((t) => ({ id: t.id, label: t.label, title: `Intervall ${t.label}` }))} />
        <span className="divider" aria-hidden="true" />
        <Seg label="Charttyp" value={type} onChange={(v) => { setType(v); writePref("chart.type", v); }}
             options={[
               { id: "candles", label: <><Icon name="candles" size={15} /><span className="visually-hidden">Kerzen</span></>, title: "Kerzen" },
               { id: "line", label: <><Icon name="line" size={15} /><span className="visually-hidden">Linie</span></>, title: "Linie" },
             ]} />
        <button type="button" className="btn sm" aria-pressed={showVol} onClick={() => { setShowVol(!showVol); writePref("chart.vol", showVol ? "0" : "1"); }}
                disabled={!hasVolume} title={hasVolume ? "Volumen ein/aus" : "Keine Volumendaten"}>
          <Icon name="volume" size={15} /> Volumen
        </button>
        <button type="button" className="btn sm" aria-pressed={showMa} onClick={() => { setShowMa(!showMa); writePref("chart.ma", showMa ? "0" : "1"); }}>
          Ø Durchschnitte
        </button>
        <span className="spacer" />
        {status && <Tag tone={status[1]}>{status[0]}</Tag>}
        {mode === "DEMO" && <Tag tone="gold">DEMO-Daten</Tag>}
        <button type="button" className="btn sm ghost" onClick={() => setFit((f) => f + 1)} title="Ansicht zurücksetzen">
          <Icon name="fit" size={15} /> <span className="visually-hidden">Ansicht zurücksetzen</span>
        </button>
      </div>
      {suggestions.length > 0 && (
        <div style={{ padding: "8px 16px 0" }}>
          <QuickSymbols items={suggestions} value={symbol} onSelect={onSymbol} max={7} />
        </div>
      )}

      <div className="chart-area">
        {data && data.bars.length > 1 && (
          <TradingChart
            bars={data.bars}
            chartType={type}
            showVolume={showVol && hasVolume}
            mas={mas}
            markers={markers}
            timeframeSec={tfSec}
            title={`${symbol} · ${TF.find((t) => t.id === tf)!.label} · ${data.asset_class === "crypto" ? "Krypto" : "Aktie/ETF"}`}
            resetKey={`${mode}:${symbol}:${tf}:${type}`}
            fitRequest={fit}
          />
        )}
        {!data && !res.error && (
          <div className="chart-overlay" aria-busy="true">
            <div className="chart-skeleton"><Skeleton height={9999} /><Skeleton height={60} /></div>
          </div>
        )}
        {!data && res.error && (
          <div className="chart-overlay">
            <EmptyState icon="alert" title="Kursdaten konnten nicht geladen werden">
              {res.error}
              <div style={{ marginTop: 10 }}><button className="btn sm" onClick={() => void res.reload()}><Icon name="refresh" size={15} /> Erneut laden</button></div>
            </EmptyState>
          </div>
        )}
        {data && data.status === "error" && (
          <div className="chart-overlay">
            <EmptyState icon="alert" title={`Keine Kurse für ${symbol}`}>
              {data.message}
              {data.error?.hint && <div style={{ marginTop: 6 }}>{data.error.hint}</div>}
              <div style={{ marginTop: 10 }}><button className="btn sm" onClick={() => void res.reload()}><Icon name="refresh" size={15} /> Erneut laden</button></div>
            </EmptyState>
          </div>
        )}
        {data && data.status !== "error" && data.bars.length <= 1 && (
          <div className="chart-overlay">
            <EmptyState title={`Keine Kerzen für ${symbol}`}>{data.message}</EmptyState>
          </div>
        )}
      </div>
      <div className="chart-foot">
        <span>Quelle: {data?.source ?? "—"}</span>
        <span>Letzte Kerze: {dateTime(data?.last_bar_at)}</span>
        <span>Abgerufen {ago(data?.fetched_at)}{data?.cached ? " (Cache)" : ""}</span>
        <span>Zeitzone: {localTz}</span>
        {data && data.status !== "ok" && data.status !== "error" && <span style={{ color: "var(--gold-text)" }}>{data.message}</span>}
      </div>
    </section>
  );
}
