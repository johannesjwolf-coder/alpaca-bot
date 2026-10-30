import { BaselineSeries, ColorType, createChart, type Time, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { signedMoney } from "../../format";
import { CHART } from "./theme";

/** Kumuliertes Nettoergebnis eines Backtests (grün über, rot unter null). */
export function EquityChart({ points }: { points: { t: string; equity: number }[] }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!box.current) return;
    const chart = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: CHART.surface }, textColor: CHART.text2, fontFamily: CHART.font, fontSize: 12, attributionLogo: true },
      grid: { vertLines: { color: CHART.grid }, horzLines: { color: CHART.grid } },
      rightPriceScale: { borderColor: CHART.line },
      timeScale: { borderColor: CHART.line, timeVisible: true },
      localization: {
        locale: "de-CH",
        priceFormatter: (p: number) => signedMoney(p, "USD"),
        timeFormatter: (t: Time) => new Date((t as number) * 1000).toLocaleString("de-CH", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }),
      },
      handleScroll: false,
      handleScale: false,
    });
    const s = chart.addSeries(BaselineSeries, {
      baseValue: { type: "price", price: 0 },
      topLineColor: CHART.posText,
      topFillColor1: "rgba(8, 153, 129, 0.18)",
      topFillColor2: "rgba(8, 153, 129, 0.02)",
      bottomLineColor: CHART.negText,
      bottomFillColor1: "rgba(242, 54, 69, 0.02)",
      bottomFillColor2: "rgba(242, 54, 69, 0.16)",
      lineWidth: 2,
    });
    s.setData(points.map((p) => ({ time: Math.floor(new Date(p.t).getTime() / 1000) as UTCTimestamp, value: p.equity })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points]);
  return <div ref={box} style={{ position: "relative", height: 220 }} aria-label="Equity-Kurve" role="img" />;
}
