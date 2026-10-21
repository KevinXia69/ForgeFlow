/** Bounded, serial reads only. Never retries a mutation or treats an SSE frame as a receipt. */
export function startCaseReadWatch<T>({ read, version, accept, onError, onExpired,
  intervalMs = 2_000, requestTimeoutMs = 10_000, lifetimeMs = 600_000,
}: {
  read: (signal: AbortSignal) => Promise<T>;
  version: () => number;
  accept: (value: T) => void;
  onError: (error: unknown) => void;
  onExpired: () => void;
  intervalMs?: number;
  requestTimeoutMs?: number;
  lifetimeMs?: number;
}): () => void {
  let stopped = false;
  let pending: AbortController | null = null;
  let next: ReturnType<typeof setTimeout> | undefined;
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const stop = () => {
    stopped = true;
    clearTimeout(next); clearTimeout(timeout); clearTimeout(deadline);
    pending?.abort();
  };
  const deadline = setTimeout(() => { stop(); onExpired(); }, lifetimeMs);
  const tick = async () => {
    const observedVersion = version();
    const controller = new AbortController();
    pending = controller;
    timeout = setTimeout(() => controller.abort(), requestTimeoutMs);
    try {
      const value = await read(controller.signal);
      // A newer mutation/refresh or navigation invalidates both old data and old errors.
      if (!stopped && observedVersion === version()) accept(value);
    } catch (error) {
      if (!stopped && observedVersion === version()) onError(error);
    } finally {
      clearTimeout(timeout);
      pending = null;
      if (!stopped) next = setTimeout(() => { void tick(); }, intervalMs);
    }
  };
  next = setTimeout(() => { void tick(); }, 0);
  return stop;
}
