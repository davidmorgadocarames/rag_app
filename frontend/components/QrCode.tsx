"use client";

// Client-side TOTP QR rendering (T12.3.5, DEC-024 = B). The secret/URI never leaves the
// browser: this component only turns a string already held in page state into an inline
// SVG, with no network call (no remote QR-image service, no CDN) and no inline
// script/eval -- `qrcode-generator` just computes a boolean module matrix; the SVG markup
// itself is built with plain React elements (never `dangerouslySetInnerHTML`), so this is
// compatible with the nonce-based `script-src 'self' 'nonce-...' 'strict-dynamic'` CSP
// (lib/csp.ts) with zero exceptions needed.
//
// Package choice (`qrcode-generator`, pinned exact in package.json): zero runtime
// dependencies (`npm view qrcode-generator` -> "deps: none"), MIT licensed, ships its own
// TypeScript declarations, actively maintained by its original author (24 published
// versions, latest a year old) and is the de-facto reference encoder most other QR npm
// packages (e.g. `qrcode.react`) wrap internally -- using it directly avoids pulling in
// those wrappers' own extra dependencies (canvas/pngjs/etc.) for a feature that only needs
// the module matrix. The actual encoding call lives in the JSX-free `lib/qrMatrix.ts` so
// `node --test` can exercise it directly (`lib/qrMatrix.test.ts`).
import type { ReactElement } from "react";

import { buildQrMatrix } from "@/lib/qrMatrix";

interface QrCodeProps {
  /** Data to encode, e.g. an `otpauth://` provisioning URI. Never logged by this component. */
  data: string;
  /** Pixel size of each QR module (default 6). */
  cellSize?: number;
  className?: string;
}

export default function QrCode({ data, cellSize = 6, className }: QrCodeProps) {
  const { moduleCount, isDark } = buildQrMatrix(data);
  const size = moduleCount * cellSize;

  const rects: ReactElement[] = [];
  for (let row = 0; row < moduleCount; row += 1) {
    for (let col = 0; col < moduleCount; col += 1) {
      if (isDark(row, col)) {
        rects.push(
          <rect
            key={`${row}-${col}`}
            x={col * cellSize}
            y={row * cellSize}
            width={cellSize}
            height={cellSize}
            fill="#000"
          />,
        );
      }
    }
  }

  return (
    <svg
      role="img"
      aria-label="Two-factor authentication QR code"
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      className={className}
    >
      <rect x={0} y={0} width={size} height={size} fill="#fff" />
      {rects}
    </svg>
  );
}
