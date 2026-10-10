"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/** Fetch `path` and keep refetching every `intervalMs` while `keepPolling(data)` is true
 * (e.g. while a job is still running). The previous data stays on screen during a refetch. */
export function usePoll<T>(path: string | null, intervalMs = 3000, keepPolling?: (data: T) => boolean) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const keep = useRef(keepPolling);
  keep.current = keepPolling;
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const next = await api<T>(path);
        if (cancelled) return;
        setData(next);
        setError(null);
        if (!keep.current || keep.current(next)) timer = setTimeout(load, intervalMs);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        timer = setTimeout(load, intervalMs * 2);
      }
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [path, intervalMs, tick]);

  return { data, error, refresh };
}

export const isActive = (status: string | undefined) => status === "queued" || status === "running";
