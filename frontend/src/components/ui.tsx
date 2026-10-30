import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { Mode } from "../types";

/* ------------------------------------------------------------------ Icons */
const PATHS: Record<string, string> = {
  overview: "M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z",
  trade: "M7 7h11l-3-3M17 17H6l3 3",
  bot: "M8 9h8a3 3 0 0 1 3 3v4a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3v-4a3 3 0 0 1 3-3zM12 5v4M10 14h.01M14 14h.01",
  backtest: "M4 19h16M6 16l4-5 3 3 5-7",
  settings:
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z",
  diagnostics: "M3 12h4l3-8 4 16 3-8h4",
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6L6 18",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
  chevron: "M6 9l6 6 6-6",
  alert: "M12 9v4M12 17h.01M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  check: "M5 12l5 5L20 7",
  play: "M7 5l12 7-12 7z",
  pause: "M8 5v14M16 5v14",
  power: "M12 3v9M6.3 6.3a8 8 0 1 0 11.4 0",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  fit: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  candles: "M7 4v16M17 4v16M5 8h4v7H5zM15 6h4v9h-4z",
  line: "M3 17l6-6 4 4 8-8",
  volume: "M5 20V12M10 20V8M15 20v-5M20 20V5",
  inbox: "M4 13l2-7h12l2 7v6H4zM4 13h5l1 2h4l1-2h5",
  info: "M12 8h.01M11 12h1v5h1M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
  up: "M12 19V5M5 12l7-7 7 7",
  down: "M12 5v14M19 12l-7 7-7-7",
};

export function Icon({ name, size = 18, className }: { name: keyof typeof PATHS | string; size?: number; className?: string }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name] ?? PATHS.info} />
    </svg>
  );
}

export function Logo() {
  return (
    <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="#232d39" />
      <path d="M6 21l6-7 5 4 9-10" stroke="#B8924A" strokeWidth="2.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/* ------------------------------------------------------------ Status */
export function Pill({ tone = "", children, title, running }: { tone?: string; children: ReactNode; title?: string; running?: boolean }) {
  return (
    <span className={`pill ${tone} ${running ? "running" : ""}`} title={title}>
      <span className="dot" aria-hidden="true" />
      {children}
    </span>
  );
}

export function ModeChip({ mode }: { mode: Mode }) {
  return (
    <span className={`mode-chip ${mode}`} title={mode === "DEMO" ? "Simulierte Daten, keine Verbindung zu Alpaca" : "Alpaca-Paper-Konto (Spielgeld)"}>
      {mode === "DEMO" ? "DEMO · simuliert" : "PAPER · Spielgeld"}
    </span>
  );
}

export function Tag({ tone = "", children }: { tone?: string; children: ReactNode }) {
  return <span className={`tag ${tone}`}>{children}</span>;
}

export function Notice({ tone = "", title, children, actions }: { tone?: string; title?: ReactNode; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className={`notice ${tone}`} role={tone === "error" ? "alert" : undefined}>
      {title && <div className="notice-title">{title}</div>}
      {children && <div>{children}</div>}
      {actions && <div className="actions row">{actions}</div>}
    </div>
  );
}

/* ------------------------------------------------------------ Struktur */
export function Card({ title, hint, actions, children, className = "", bodyClass = "card-body", foot }: {
  title?: ReactNode; hint?: ReactNode; actions?: ReactNode; children?: ReactNode; className?: string; bodyClass?: string | null; foot?: ReactNode;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions || hint) && (
        <div className="card-head">
          {title && <h2>{title}</h2>}
          {hint && <span className="hint">{hint}</span>}
          {actions && <div className="row" style={{ marginLeft: hint ? 0 : "auto" }}>{actions}</div>}
        </div>
      )}
      {bodyClass === null ? children : <div className={bodyClass}>{children}</div>}
      {foot && <div className="card-foot">{foot}</div>}
    </section>
  );
}

export function Kpi({ label, value, unit, sub, loading }: { label: ReactNode; value: ReactNode; unit?: string; sub?: ReactNode; loading?: boolean }) {
  return (
    <div className="card kpi">
      <div className="kpi-label">{label}</div>
      {loading ? (
        <>
          <div className="skeleton" style={{ height: 30, width: "70%" }} />
          <div className="skeleton" style={{ height: 12, width: "50%" }} />
        </>
      ) : (
        <>
          <div className="kpi-value">{value}{unit && <span className="ccy">{unit}</span>}</div>
          {sub && <div className="kpi-sub">{sub}</div>}
        </>
      )}
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, label }: {
  tabs: { id: T; label: string; count?: number }[]; value: T; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button key={t.id} role="tab" className="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}>
          {t.label}
          {t.count !== undefined && <span className="count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Seg<T extends string>({ options, value, onChange, label, className = "", disabled }: {
  options: { id: T; label: ReactNode; side?: string; disabled?: boolean; title?: string }[];
  value: T; onChange: (v: T) => void; label: string; className?: string; disabled?: boolean;
}) {
  return (
    <div className={`seg ${className}`} role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.id} type="button" aria-pressed={value === o.id} data-side={o.side} title={o.title}
                disabled={disabled || o.disabled} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Skeleton({ height = 16, width = "100%" }: { height?: number; width?: number | string }) {
  return <div className="skeleton" style={{ height, width }} />;
}

export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="stack" style={{ padding: 20 }}>
      {Array.from({ length: rows }, (_, i) => <Skeleton key={i} height={14} width={`${90 - i * 8}%`} />)}
    </div>
  );
}

export function EmptyState({ icon = "inbox", title, children }: { icon?: string; title: ReactNode; children?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-icon"><Icon name={icon} /></div>
      <strong>{title}</strong>
      {children && <div className="sm">{children}</div>}
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

/* ------------------------------------------------------------ Bestätigung */
interface ConfirmOptions {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "buy" | "sell" | "danger";
  cancelLabel?: string | null; // null = nur Bestätigen (Hinweisdialog)
}
type ConfirmFn = (o: ConfirmOptions) => Promise<boolean>;
const ConfirmCtx = createContext<ConfirmFn>(async () => false);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<(ConfirmOptions & { resolve: (v: boolean) => void }) | null>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const confirm = useCallback<ConfirmFn>((o) => new Promise((resolve) => setState({ ...o, resolve })), []);
  const close = (v: boolean) => {
    state?.resolve(v);
    setState(null);
  };
  useEffect(() => {
    if (!state) return;
    confirmRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);
  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      {state && (
        <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && close(false)}>
          <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
            <div className="dialog-head"><h2 id="dlg-title">{state.title}</h2></div>
            <div className="dialog-body">{state.body}</div>
            <div className="dialog-foot">
              {state.cancelLabel !== null && (
                <button className="btn" onClick={() => close(false)}>{state.cancelLabel ?? "Abbrechen"}</button>
              )}
              <button ref={confirmRef} className={`btn ${state.tone ?? "primary"}`} onClick={() => close(true)}>
                {state.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmCtx.Provider>
  );
}

export const useConfirm = () => useContext(ConfirmCtx);
