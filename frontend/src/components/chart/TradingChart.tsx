import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { price, priceDigits } from "../../format";
import type { BarDto } from "../../types";
import { CHART } from "./theme";

export interface ChartMarker {
  time: string; // ISO
  side: "buy" | "sell";
  label: string; // Tooltip-Text in der Legende
}

export interface MaSpec {
  period: number;
  color: string;
  label: string;
  startIndex?: number; // z. B. Sitzungsbeginn: erst ab hier rechnen
}

interface Props {
  bars: BarDto[];
  chartType: "candles" | "line";
  showVolume: boolean;
  mas: MaSpec[];
  markers: ChartMarker[];
  timeframeSec: number;
  title: string;
  resetKey: string;
  fitRequest?: number;
}

const toTs = (iso: string) => Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
const fromTs = (t: Time) => new Date((t as number) * 1000);

const TIME_FMT = new Intl.DateTimeFormat("de-CH", { hour: "2-digit", minute: "2-digit" });
const DAY_FMT = new Intl.DateTimeFormat("de-CH", { day: "2-digit", month: "2-digit" });
const MONTH_FMT = new Intl.DateTimeFormat("de-CH", { month: "short", year: "2-digit" });
const FULL_FMT = new Intl.DateTimeFormat("de-CH", {
  weekday: "short", day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit", timeZoneName: "short",
});

function smaValues(bars: BarDto[], n: number, start = 0): (number | null)[] {
  let sum = 0;
  return bars.map((b, i) => {
    if (i < start) return null;
    sum += b.c;
    if (i - start >= n) sum -= bars[i - n].c;
    return i - start >= n - 1 ? sum / n : null;
  });
}

interface Hover {
  bar: BarDto;
  prevClose: number | null;
  ma: (number | null)[];
  markers: ChartMarker[];
}

/** Professioneller Kerzenchart (TradingView Lightweight Charts™, Apache-2.0, Attribution im Chart). */
export function TradingChart({ bars, chartType, showVolume, mas, markers, timeframeSec, title, resetKey, fitRequest }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const mainRef = useRef<ISeriesApi<"Candlestick"> | ISeriesApi<"Line"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const maRefs = useRef<ISeriesApi<"Line">[]>([]);
  const markerApi = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const lastKey = useRef<string>("");
  const [hover, setHover] = useState<Hover | null>(null);
  const [seriesVersion, setSeriesVersion] = useState(0);

  const maData = useMemo(() => mas.map((m) => smaValues(bars, m.period, m.startIndex ?? 0)), [bars, mas]);
  const markersByBar = useMemo(() => {
    const map = new Map<number, ChartMarker[]>();
    if (!bars.length) return map;
    const times = bars.map((b) => toTs(b.t) as number);
    for (const m of markers) {
      const t = toTs(m.time) as number;
      if (t < times[0] || t >= times[times.length - 1] + timeframeSec) continue;
      let lo = 0;
      let hi = times.length - 1;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (times[mid] <= t) lo = mid;
        else hi = mid - 1;
      }
      const key = times[lo];
      map.set(key, [...(map.get(key) ?? []), m]);
    }
    return map;
  }, [markers, bars, timeframeSec]);

  // Chart einmalig anlegen
  useEffect(() => {
    if (!box.current) return;
    const chart = createChart(box.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: CHART.surface },
        textColor: CHART.text2,
        fontFamily: CHART.font,
        fontSize: 12,
        attributionLogo: true, // Lizenz: Link zu tradingview.com
      },
      grid: { vertLines: { color: CHART.grid }, horzLines: { color: CHART.grid } },
      rightPriceScale: { borderColor: CHART.line, scaleMargins: { top: 0.1, bottom: 0.24 } },
      timeScale: {
        borderColor: CHART.line,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 4,
        tickMarkFormatter: (time: Time, type: TickMarkType) => {
          const d = fromTs(time);
          if (type === TickMarkType.Year || type === TickMarkType.Month) return MONTH_FMT.format(d);
          if (type === TickMarkType.DayOfMonth) return DAY_FMT.format(d);
          return TIME_FMT.format(d);
        },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: CHART.text2, style: LineStyle.Dashed, width: 1, labelBackgroundColor: CHART.text },
        horzLine: { color: CHART.text2, style: LineStyle.Dashed, width: 1, labelBackgroundColor: CHART.text },
      },
      localization: {
        locale: "de-CH",
        priceFormatter: (p: number) => price(p),
        timeFormatter: (t: Time) => FULL_FMT.format(fromTs(t)),
      },
      handleScroll: true,
      handleScale: true,
    });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      mainRef.current = null;
      volRef.current = null;
      maRefs.current = [];
      markerApi.current = null;
    };
  }, []);

  // Serien je nach Charttyp/Volumen/MAs neu aufbauen
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    for (const s of [mainRef.current, volRef.current, ...maRefs.current]) if (s) chart.removeSeries(s);
    markerApi.current = null;
    const main =
      chartType === "candles"
        ? chart.addSeries(CandlestickSeries, {
            upColor: CHART.up, downColor: CHART.down, borderUpColor: CHART.up, borderDownColor: CHART.down,
            wickUpColor: CHART.up, wickDownColor: CHART.down, priceLineColor: CHART.text2,
          })
        : chart.addSeries(LineSeries, { color: CHART.text, lineWidth: 2, priceLineColor: CHART.text2 });
    mainRef.current = main;
    volRef.current = showVolume
      ? chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false })
      : null;
    if (volRef.current) chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.1, bottom: showVolume ? 0.22 : 0.08 } });
    maRefs.current = mas.map((m) =>
      chart.addSeries(LineSeries, {
        color: m.color, lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
      }),
    );
    lastKey.current = ""; // nach Neuaufbau Daten vollständig setzen
    setSeriesVersion((v) => v + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartType, showVolume, mas.map((m) => `${m.period}:${m.color}:${m.startIndex ?? 0}`).join("|")]);

  // Daten setzen, Ansicht beim Nachladen beibehalten
  useEffect(() => {
    const chart = chartRef.current;
    const main = mainRef.current;
    if (!chart || !main) return;
    const ts = chart.timeScale();
    const keyChanged = lastKey.current !== resetKey;
    const prevRange = keyChanged ? null : ts.getVisibleLogicalRange();
    const prevLen = (main.data() ?? []).length;
    const atEnd = prevRange ? prevRange.to >= prevLen - 2 : true;
    const prevTimeRange = keyChanged ? null : ts.getVisibleRange();

    const last = bars[bars.length - 1];
    if (last) {
      const d = priceDigits(last.c);
      main.applyOptions({ priceFormat: { type: "price", precision: d, minMove: 1 / 10 ** d } });
    }
    if (chartType === "candles") {
      (main as ISeriesApi<"Candlestick">).setData(bars.map((b) => ({ time: toTs(b.t), open: b.o, high: b.h, low: b.l, close: b.c })));
    } else {
      (main as ISeriesApi<"Line">).setData(bars.map((b) => ({ time: toTs(b.t), value: b.c })));
    }
    volRef.current?.setData(
      bars.map((b) => ({ time: toTs(b.t), value: b.v, color: b.c >= b.o ? CHART.upVol : CHART.downVol })),
    );
    maRefs.current.forEach((s, i) =>
      s.setData(
        bars.flatMap((b, j) => (maData[i]?.[j] != null ? [{ time: toTs(b.t), value: maData[i][j] as number }] : [])),
      ),
    );
    const seriesMarkers: SeriesMarker<Time>[] = [];
    for (const [t, list] of [...markersByBar.entries()].sort((a, b) => a[0] - b[0])) {
      const buys = list.filter((m) => m.side === "buy").length;
      const sells = list.length - buys;
      if (buys)
        seriesMarkers.push({ time: t as UTCTimestamp, position: "belowBar", shape: "arrowUp", color: CHART.posText, text: buys > 1 ? `K×${buys}` : "K" });
      if (sells)
        seriesMarkers.push({ time: t as UTCTimestamp, position: "aboveBar", shape: "arrowDown", color: CHART.negText, text: sells > 1 ? `V×${sells}` : "V" });
    }
    if (!markerApi.current) markerApi.current = createSeriesMarkers(main as ISeriesApi<"Candlestick">, seriesMarkers);
    else markerApi.current.setMarkers(seriesMarkers);

    if (keyChanged) {
      ts.fitContent();
      if (bars.length > 160) ts.setVisibleLogicalRange({ from: bars.length - 160, to: bars.length + 4 });
      lastKey.current = resetKey;
    } else if (atEnd) {
      ts.scrollToRealTime();
    } else if (prevTimeRange) {
      try {
        ts.setVisibleRange(prevTimeRange);
      } catch {
        ts.scrollToRealTime();
      }
    }
  }, [bars, maData, markersByBar, chartType, resetKey, seriesVersion]);

  // Zoom zurücksetzen
  useEffect(() => {
    if (fitRequest && chartRef.current) chartRef.current.timeScale().fitContent();
  }, [fitRequest]);

  // Fadenkreuz → Legende
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    let frame = 0;
    const handler = (p: MouseEventParams<Time>) => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (!p.time || !p.point) {
          setHover(null);
          return;
        }
        const t = p.time as number;
        const idx = bars.findIndex((b) => toTs(b.t) === t);
        if (idx < 0) return setHover(null);
        setHover({
          bar: bars[idx],
          prevClose: idx > 0 ? bars[idx - 1].c : null,
          ma: maData.map((m) => m[idx] ?? null),
          markers: markersByBar.get(t) ?? [],
        });
      });
    };
    chart.subscribeCrosshairMove(handler);
    return () => {
      cancelAnimationFrame(frame);
      chart.unsubscribeCrosshairMove(handler);
    };
  }, [bars, maData, markersByBar]);

  const shown = hover ?? (bars.length ? {
    bar: bars[bars.length - 1],
    prevClose: bars.length > 1 ? bars[bars.length - 2].c : null,
    ma: maData.map((m) => m[m.length - 1] ?? null),
    markers: [] as ChartMarker[],
  } : null);
  const chg = shown && shown.prevClose ? (shown.bar.c / shown.prevClose - 1) * 100 : null;
  const dir = shown ? (shown.bar.c >= shown.bar.o ? "pos" : "neg") : "";

  return (
    <>
      <div className="chart-canvas" ref={box} />
      {shown && (
        <div className="chart-legend" aria-live="off">
          <div className="l-title">{title}</div>
          <div className="ohlc">
            <span>E <b className={dir}>{price(shown.bar.o)}</b></span>
            <span>H <b className={dir}>{price(shown.bar.h)}</b></span>
            <span>T <b className={dir}>{price(shown.bar.l)}</b></span>
            <span>S <b className={dir}>{price(shown.bar.c)}</b></span>
            {chg !== null && <span className={chg >= 0 ? "pos" : "neg"}>{chg >= 0 ? "+" : "−"}{Math.abs(chg).toFixed(2)} %</span>}
            {showVolume && <span>Vol <b>{new Intl.NumberFormat("de-CH", { notation: "compact" }).format(shown.bar.v)}</b></span>}
          </div>
          {mas.length > 0 && (
            <div className="ma">
              {mas.map((m, i) => (
                <span key={m.label}>
                  <i className="swatch" style={{ background: m.color }} />
                  {m.label} <b>{shown.ma[i] != null ? price(shown.ma[i]) : "—"}</b>
                </span>
              ))}
            </div>
          )}
          {shown.markers.map((m, i) => (
            <div key={i} className={`marker-info ${m.side === "buy" ? "pos" : "neg"}`}>{m.label}</div>
          ))}
        </div>
      )}
    </>
  );
}
