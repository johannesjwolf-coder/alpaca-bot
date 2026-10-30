import { useState } from "react";
import { api, errorText } from "../api";
import type { EmergencyResult, Status } from "../types";

/** Not-Aus: Bot stoppen, offene Orders stornieren, alle Positionen schliessen. */
export function EmergencyButton({ status, onDone }: { status: Status; onDone: () => void }) {
  const [busy, setBusy] = useState(false);

  async function trigger() {
    const sellNote =
      status.mode === "PAPER" && !status.orders_enabled
        ? "\n\nHinweis: Die Paper-Orderfreigabe ist aus – Positionen werden dann NICHT geschlossen, nur Orders storniert."
        : "";
    if (!window.confirm(`NOT-AUS (${status.mode})?\n\n• Bot sofort pausieren\n• alle offenen Orders stornieren\n• alle Positionen per Market-Order schliessen${sellNote}`)) return;
    setBusy(true);
    try {
      const r = await api.post<EmergencyResult>("/emergency-stop", { expected_mode: status.mode });
      const lines = [
        `Bot pausiert.`,
        `Storniert: ${r.canceled.length} Order(s).`,
        `Geschlossen: ${r.closed.map((c) => c.symbol).join(", ") || "keine"}.`,
      ];
      if (r.not_closed.length) lines.push(`NICHT geschlossen: ${r.not_closed.map((c) => `${c.symbol} (${c.reason})`).join(", ")}.`);
      if (r.error) lines.push(r.error);
      if (r.cancel_error) lines.push(`Stornierung: ${r.cancel_error}`);
      window.alert(lines.join("\n"));
    } catch (e) {
      window.alert(`Not-Aus fehlgeschlagen: ${errorText(e)}`);
    } finally {
      setBusy(false);
      onDone();
    }
  }

  return (
    <button className="btn danger" onClick={trigger} disabled={busy} title="Bot stoppen, Orders stornieren, Positionen schliessen">
      {busy ? "Not-Aus läuft…" : "Not-Aus"}
    </button>
  );
}
