import assert from "node:assert/strict";
import { test } from "node:test";

import { hasSessionCookie, isProtectedPath, shouldRedirectToLogin } from "./authGuard.ts";

test("chat and account (and their sub-paths) are protected; everything else is not", () => {
  assert.equal(isProtectedPath("/chat"), true);
  assert.equal(isProtectedPath("/chat/anything"), true);
  assert.equal(isProtectedPath("/account"), true);
  assert.equal(isProtectedPath("/account/security"), true);
  assert.equal(isProtectedPath("/"), false);
  assert.equal(isProtectedPath("/login"), false);
  // a path that merely starts with the same letters is NOT protected (no false positive)
  assert.equal(isProtectedPath("/chatbot"), false);
  assert.equal(isProtectedPath("/accounting"), false);
});

test("the session cookie's mere presence is detected, whatever its value", () => {
  assert.equal(hasSessionCookie(null), false);
  assert.equal(hasSessionCookie(undefined), false);
  assert.equal(hasSessionCookie(""), false);
  assert.equal(hasSessionCookie("other=1"), false);
  assert.equal(hasSessionCookie("__Host-secrag_session=abc"), true);
  assert.equal(hasSessionCookie("other=1; __Host-secrag_session=abc; third=2"), true);
  // a forged/garbage value still counts as "present" -- this guard is UX only (D8), never
  // the security boundary; the backend is what actually verifies the session.
  assert.equal(hasSessionCookie("__Host-secrag_session=not-a-real-session-id"), true);
});

test("redirects to login only on a protected path with no session cookie", () => {
  assert.equal(shouldRedirectToLogin("/chat", null), true);
  assert.equal(shouldRedirectToLogin("/account", ""), true);
  assert.equal(shouldRedirectToLogin("/chat", "__Host-secrag_session=abc"), false);
  assert.equal(shouldRedirectToLogin("/login", null), false);
  assert.equal(shouldRedirectToLogin("/", null), false);
});
