import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The site is static: every section is prerendered and the only dynamic
  // thing on the page is the live call, which talks to Railway from the
  // browser. That keeps Vercel serving HTML from the edge and keeps the
  // Python backend the single place a caller's words are ever handled.
  output: "export",
  images: { unoptimized: true },
  trailingSlash: true,
};

export default config;
