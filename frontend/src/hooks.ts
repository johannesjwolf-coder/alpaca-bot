import { useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "./api";

export interface Polled<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
}

/** Ruft ``fn`` sofort und danach im Intervall auf. Ältere Antworten überschreiben keine neueren. */
export function usePoll<T>(fn: () => Promise<T>, intervalMs: number, deps: unknown[] = []): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const reload = useCallback(async () => {
    const id = ++seq.current;
    try {
      const value = await fnRef.current();
      if (id === seq.current) {
        setData(value);
        setError(null);
      }
    } catch (e) {
      if (id === seq.current) setError(errorText(e));
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    setData(null);
    void reload();
    const t = window.setInterval(() => void reload(), intervalMs);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, reload, ...deps]);

  return { data, error, loading, reload };
}
