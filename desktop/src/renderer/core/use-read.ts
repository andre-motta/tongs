import { useCallback, useEffect, useState, type DependencyList } from "react";
import type { DesktopRead } from "../../shared/bridge.js";
import {
  QueryCoordinator,
  StaleQueryError,
  type CoordinatedRead,
} from "./query.js";

export interface ReadState<T> {
  readonly value: T | null;
  readonly error: unknown;
  readonly loading: boolean;
  readonly refresh: () => void;
}

export function useRetainedRead<T>(
  coordinator: QueryCoordinator,
  key: string,
  begin: () => DesktopRead<T> | CoordinatedRead<T>,
  dependencies: DependencyList,
  enabled = true,
): ReadState<T> {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const refresh = useCallback(() => setReload((current) => current + 1), []);

  useEffect(() => {
    if (!enabled) {
      setLoading(true);
      setError(null);
      return;
    }
    let current = true;
    setLoading(true);
    setError(null);
    void coordinator
      .run(key, begin)
      .then((next) => {
        if (current) {
          setValue(next);
          setLoading(false);
        }
      })
      .catch((reason: unknown) => {
        if (current && !(reason instanceof StaleQueryError)) {
          setError(reason);
          setLoading(false);
        }
      });
    return () => {
      current = false;
      void coordinator.cancel(key);
    };
  }, [coordinator, enabled, key, reload, ...dependencies]);
  return { value, error, loading, refresh };
}
