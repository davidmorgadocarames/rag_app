// Pure QR-encoding wrapper behind components/QrCode.tsx (T12.3.5, DEC-024 = B). Kept
// Next.js/JSX-free so `node --test` can exercise the actual encoding (not just a mock) the
// same way `lib/authGuard.ts`/`lib/bffRoutes.ts` do (C5 pattern) -- the component itself
// only turns this matrix into SVG <rect> elements, with no JSX involved on this side.
import qrcode from "qrcode-generator";

export interface QrMatrix {
  moduleCount: number;
  isDark: (row: number, col: number) => boolean;
}

/** Encodes `data` (e.g. an `otpauth://` URI) into a QR module matrix. Never logs `data`. */
export function buildQrMatrix(data: string): QrMatrix {
  const qr = qrcode(0, "M");
  qr.addData(data);
  qr.make();
  return {
    moduleCount: qr.getModuleCount(),
    isDark: (row, col) => qr.isDark(row, col),
  };
}
