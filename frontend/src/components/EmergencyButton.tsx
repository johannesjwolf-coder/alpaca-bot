import { useState } from "react";
import { api, errorText } from "../api";
import type { EmergencyResult, Status } from "../types";
import { Icon, Spinner, useConfirm } from "./ui";

/** Not-Aus: Bot pausieren, offene Orders stornieren, alle Positionen schliessen. */
export function EmergencyButton({ status, onDone }: { status: Status; onDone: () => void }) {
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);

  async function trigger() {
    const noSell = status.mode === "PAPER" && !status.orders_enabled;
    const ok = await confirm({
      title: `Not-Aus im ${status.mode}-Modus?`,
      tone: "sell",
      confirmLabel: "Not-Aus auslösen",
      body: (
        <>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            <li>Bot sofort pausieren</li>
            <li>alle offenen Orders stornieren</li>
            <li>alle Positionen per Market-Order schliessen</li>
          </ul>
          {noSell && <p className="neg">Die Paper-Orderfreigabe ist aus: Positionen werden nicht geschlossen, nur Orders storniert.</p>}
        </>
      ),
    });
    if (!ok) return;
    setBusy(true);
    let body: React.ReactNode;
    try {
      const r = await api.post<EmergencyResult>("/emergency-stop", { expected_mode: status.mode });
      body = (
        <dl className="kv">
          <dt>Bot</dt><dd>pausiert</dd>
          <dt>Storniert</dt><dd>{r.canceled.length} Order(s){r.cancel_error ? ` · ${r.cancel_error}` : ""}</dd>
          <dt>Geschlossen</dt><dd>{r.closed.map((c) => c.symbol).join(", ") || "keine Positionen"}</dd>
          {r.not_closed.length > 0 && <><dt>Nicht geschlossen</dt><dd className="neg">{r.not_closed.map((c) => `${c.symbol} (${c.reason})`).join(", ")}</dd></>}
          {r.error && <><dt>Hinweis</dt><dd className="neg">{r.error}</dd></>}
        </dl>
      );
    } catch (e) {
      body = <p className="neg">Not-Aus fehlgeschlagen: {errorText(e)}</p>;
    } finally {
      setBusy(false);
      onDone();
    }
    await confirm({ title: "Not-Aus ausgeführt", body, confirmLabel: "OK", cancelLabel: null });
  }

  return (
    <button className="btn sell sm" onClick={trigger} disabled={busy} title="Bot stoppen, Orders stornieren, Positionen schliessen">
      {busy ? <Spinner /> : <Icon name="power" size={15} />} Not-Aus
    </button>
  );
}
