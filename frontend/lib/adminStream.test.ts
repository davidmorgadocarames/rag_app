import assert from "node:assert/strict";
import { test } from "node:test";

import { closedReasonMessage, parseAdminStreamData, sortAlertsBySeverity } from "./adminStream.ts";

test("parseAdminStreamData parses a real snapshot payload", () => {
  const data = parseAdminStreamData('{"features": [], "erasure": {"requests": []}}');
  assert.deepEqual(data, { features: [], erasure: { requests: [] } });
});

test("parseAdminStreamData returns null on malformed JSON, never throws", () => {
  assert.equal(parseAdminStreamData("not json"), null);
});

test("parseAdminStreamData returns null for a non-object JSON value", () => {
  assert.equal(parseAdminStreamData("42"), null);
  assert.equal(parseAdminStreamData('"a string"'), null);
  assert.equal(parseAdminStreamData("null"), null);
});

test("closedReasonMessage covers every backend reason with a distinct message", () => {
  const reasons = ["session_expired", "role_lost", "mfa_step_up_lost", "idle_timeout"];
  const messages = reasons.map(closedReasonMessage);
  assert.equal(new Set(messages).size, messages.length);
  for (const message of messages) assert.ok(message.length > 0);
});

test("closedReasonMessage falls back to a generic message for an unknown reason", () => {
  assert.equal(
    closedReasonMessage("something-new"),
    "The live connection closed. Reload the page to reconnect.",
  );
});

test("sortAlertsBySeverity puts every red alert before every amber one", () => {
  const alerts = [
    { code: "lockout_spike", severity: "amber" },
    { code: "erasure_overdue", severity: "red" },
    { code: "email_failures", severity: "amber" },
    { code: "daily_cap_reached", severity: "red" },
  ];
  const sorted = sortAlertsBySeverity(alerts);
  assert.deepEqual(
    sorted.map((a) => a.code),
    ["erasure_overdue", "daily_cap_reached", "lockout_spike", "email_failures"],
  );
});

test("sortAlertsBySeverity is stable within the same severity", () => {
  const alerts = [
    { code: "a", severity: "amber" },
    { code: "b", severity: "amber" },
  ];
  assert.deepEqual(
    sortAlertsBySeverity(alerts).map((a) => a.code),
    ["a", "b"],
  );
});

test("sortAlertsBySeverity never mutates its input and sorts an unknown severity last", () => {
  const alerts = [
    { code: "weird", severity: "mystery" },
    { code: "red-one", severity: "red" },
  ];
  const original = [...alerts];
  const sorted = sortAlertsBySeverity(alerts);
  assert.deepEqual(alerts, original); // input untouched
  assert.deepEqual(
    sorted.map((a) => a.code),
    ["red-one", "weird"],
  );
});
