import { beforeEach } from "vitest";
import { limiters } from "@/lib/ratelimit";

// The limiters are module-level singletons: start every test with empty counters.
beforeEach(() => {
  for (const limiter of Object.values(limiters)) limiter.reset();
});
