// TOTP code generation for e2e tests (T13.2.10): the backend hands enrolment a real base32
// secret (``pyotp.random_base32()``, see ``rag_app.mfa.generate_totp_secret``) and verifies
// with ``pyotp`` defaults (SHA1, 30s step, 6 digits, RFC 6238) -- this computes the SAME
// code from that secret so a Playwright spec can complete MFA enrolment/confirm/step-up for
// real, with no backend test hook and no secret ever hand-typed into a test.
//
// Node-only (``node:crypto``): this file runs inside Playwright's own Node test process,
// never in the browser -- there is nothing here for the browser bundle/CSP to see.

import { createHmac } from "node:crypto";

const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

/** RFC 4648 base32 decode (no padding required) -- the shape pyotp's secrets use. */
export function base32Decode(input: string): Buffer {
  const clean = input.replace(/=+$/, "").toUpperCase();
  let bits = "";
  for (const char of clean) {
    const value = BASE32_ALPHABET.indexOf(char);
    if (value === -1) throw new Error(`invalid base32 character: ${char}`);
    bits += value.toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(Number.parseInt(bits.slice(i, i + 8), 2));
  }
  return Buffer.from(bytes);
}

export interface TotpOptions {
  nowMs?: number; // defaults to Date.now()
  stepSeconds?: number; // pyotp default: 30
  digits?: number; // pyotp default: 6
}

/** RFC 6238 TOTP (HMAC-SHA1, pyotp's defaults) from a base32 secret. */
export function totpCode(secretBase32: string, options: TotpOptions = {}): string {
  const stepSeconds = options.stepSeconds ?? 30;
  const digits = options.digits ?? 6;
  const nowMs = options.nowMs ?? Date.now();
  const counter = Math.floor(nowMs / 1000 / stepSeconds);

  const counterBuffer = Buffer.alloc(8);
  counterBuffer.writeBigUInt64BE(BigInt(counter));

  const hmac = createHmac("sha1", base32Decode(secretBase32)).update(counterBuffer).digest();
  const offset = hmac[hmac.length - 1] & 0x0f;
  const binary =
    ((hmac[offset] & 0x7f) << 24) |
    ((hmac[offset + 1] & 0xff) << 16) |
    ((hmac[offset + 2] & 0xff) << 8) |
    (hmac[offset + 3] & 0xff);
  const code = (binary % 10 ** digits).toString().padStart(digits, "0");
  return code;
}
