import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BACK_TO_LOGIN_HREF,
  BACK_TO_LOGIN_LABEL,
  NOT_FOUND_BODY,
  NOT_FOUND_CODE,
  NOT_FOUND_HEADING,
  NOT_FOUND_LABEL,
} from "./notFoundContent.ts";

test("the only destination this page offers is /login", () => {
  assert.equal(BACK_TO_LOGIN_HREF, "/login");
  assert.equal(BACK_TO_LOGIN_LABEL, "Back to login");
});

test("no field ever carries a request path, a query string or another link's target", () => {
  // Structural proof behind T13.10.3's "no path echo": this module has exactly one
  // href-shaped export, and it is the fixed /login constant above -- there is no second
  // link target and no placeholder (e.g. "{path}") any caller could fill in with the
  // requested URL.
  const values = [NOT_FOUND_CODE, NOT_FOUND_LABEL, NOT_FOUND_HEADING, NOT_FOUND_BODY];
  for (const v of values) {
    assert.doesNotMatch(v, /\{|%s|href=|http:\/\/|https:\/\//i, `unexpected path-shaped content: ${v}`);
  }
});

test("heading and body are plain, non-empty copy", () => {
  assert.ok(NOT_FOUND_HEADING.length > 0);
  assert.ok(NOT_FOUND_BODY.length > 0);
  assert.equal(NOT_FOUND_CODE, "404");
});
