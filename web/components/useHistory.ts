"use client";

import { useSyncExternalStore } from "react";
import { browserStorage, HISTORY_EVENT, HISTORY_KEY, loadHistory, type HistoryEntry } from "@/lib/history";

// localStorage as an external store: every component sees the same list, updated on each write (HISTORY_EVENT) and
// when another tab changes it ("storage" event). The server has no history, so it renders null.

let cachedRaw: string | null | undefined;
let cachedEntries: HistoryEntry[] = [];

function snapshot(): HistoryEntry[] {
  let raw: string | null = null;
  try {
    raw = browserStorage()?.getItem(HISTORY_KEY) ?? null;
  } catch {
    raw = null;
  }
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    cachedEntries = loadHistory(browserStorage()); // same array until the stored text changes
  }
  return cachedEntries;
}

function subscribe(onChange: () => void): () => void {
  const onStorage = (e: StorageEvent) => (e.key === null || e.key === HISTORY_KEY) && onChange();
  window.addEventListener(HISTORY_EVENT, onChange);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(HISTORY_EVENT, onChange);
    window.removeEventListener("storage", onStorage);
  };
}

export function useHistory(): HistoryEntry[] | null {
  return useSyncExternalStore(subscribe, snapshot, () => null);
}
