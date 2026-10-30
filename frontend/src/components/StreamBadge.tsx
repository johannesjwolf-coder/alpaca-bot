import { ago } from "../format";
import type { StreamState, StreamsInfo } from "../types";
import { Tag } from "./ui";

function until(iso: string): string {
  const s = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 1000));
  return s < 60 ? `in ${s} s` : `in ${Math.round(s / 60)} min`;
}

export const STREAM_STATE: Record<StreamState["state"], [string, string]> = {
  live: ["verbunden", "ok"],
  connecting: ["verbindet …", "info"],
  reconnecting: ["getrennt – neuer Versuch", "warn"],
  unavailable: ["nicht verfügbar", ""],
  off: ["aus", ""],
};

/** Kurzanzeige, woher der Bot seine Kurse gerade bekommt. */
export function FeedTag({ streams, feed }: { streams: StreamsInfo | undefined; feed: string | undefined }) {
  if (feed === "stream") return <Tag tone="ok">Echtzeit-Datenstrom</Tag>;
  if (feed === "mixed") return <Tag tone="warn">Strom + Abfrage</Tag>;
  const market = streams?.market;
  if (streams?.available && market && market.state !== "off" && market.state !== "live")
    return <Tag tone="warn">Abfrage (Strom {market.state === "connecting" ? "verbindet" : "getrennt"})</Tag>;
  return <Tag>Abfrage</Tag>;
}

export function StreamLine({ label, s }: { label: string; s: StreamState }) {
  const [text, tone] = STREAM_STATE[s.state] ?? [s.state, ""];
  return (
    <>
      <dt>{label}</dt>
      <dd>
        <Tag tone={tone}>{text}</Tag>
        {s.state === "live" && s.last_message_at && <span className="muted small"> letzte Nachricht {ago(s.last_message_at)}</span>}
        {s.state === "reconnecting" && s.retry_at && <span className="muted small"> nächster Versuch {until(s.retry_at)}</span>}
        {s.last_error && s.state !== "live" && <div className="muted small">{s.last_error}</div>}
      </dd>
    </>
  );
}
