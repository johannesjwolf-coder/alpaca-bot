import { useMemo } from "react";
import { ChartPanel, ordersToMarkers } from "../components/chart/ChartPanel";
import { OrderTicket } from "../components/OrderTicket";
import { useSymbolSuggestions } from "../components/SymbolPicker";
import type { Dashboard, Status } from "../types";
import { BottomTables, ConnectionNotice } from "./Overview";
import { api } from "../api";

/** Handel: Chart und ausführliches Order-Ticket mit allen Prüfungen. */
export function TradePage({ status, dash, symbol, onSymbol, refresh, onNavigate }: {
  status: Status; dash: { data: Dashboard | null }; symbol: string; onSymbol: (s: string) => void;
  refresh: () => void; onNavigate: (p: string) => void;
}) {
  const suggestions = useSymbolSuggestions(status.mode);
  const data = dash.data && dash.data.mode === status.mode ? dash.data : null;
  const markers = useMemo(() => ordersToMarkers(data?.orders ?? [], symbol, data?.account?.currency ?? null), [data?.orders, symbol, data?.account?.currency]);
  return (
    <>
      <ConnectionNotice data={data} onRetry={() => void api.post("/sync").then(refresh, refresh)} onNavigate={onNavigate} />
      <div className="workspace">
        <ChartPanel mode={status.mode} symbol={symbol} onSymbol={onSymbol} suggestions={suggestions.data?.items ?? []} markers={markers} />
        <OrderTicket status={status} symbol={symbol} onSymbol={onSymbol} onDone={refresh} variant="full" onNavigate={onNavigate} />
      </div>
      <BottomTables data={data} onDone={refresh} />
    </>
  );
}
