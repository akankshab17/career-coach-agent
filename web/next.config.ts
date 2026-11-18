import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The end-to-end test builds into its own folder, so it never clashes with a running `next dev` (both used .next/).
  distDir: process.env.NEXT_DIST_DIR || ".next",
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
