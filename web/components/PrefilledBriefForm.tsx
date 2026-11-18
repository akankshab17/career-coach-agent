"use client";

import { useSyncExternalStore } from "react";
import { readPrefill } from "@/lib/history";
import { BriefForm } from "./BriefForm";

// "Build again" links put an earlier brief's inputs in the query string (lib/history.ts rebuildUrl). The page stays
// static: the server renders an empty form, and once hydrated the form remounts (new key) with the prefill.
const noSubscribe = () => () => {};

export function PrefilledBriefForm() {
  const search = useSyncExternalStore(noSubscribe, () => window.location.search, () => "");
  return <BriefForm key={search} prefill={readPrefill(search)} />;
}
