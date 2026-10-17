import { SECURITY_HEADERS } from "./lib/securityHeaders.mjs";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  // F2/T12.2.1: no `X-Powered-By: Next.js` response header.
  poweredByHeader: false,
  // T12.2.1: HSTS/nosniff/Referrer-Policy/Permissions-Policy on every response, including
  // `_next/static` assets -- the per-request CSP (nonce) lives in middleware.ts/lib/csp.ts
  // instead, since it needs a fresh value per request.
  async headers() {
    return [{ source: "/:path*", headers: SECURITY_HEADERS }];
  },
};

export default nextConfig;
