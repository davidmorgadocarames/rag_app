// T13.8.5/T13.8.4: a single shared badge for every status/severity shown on the dashboard --
// icon + text together (never colour alone, accessibility rule from the approved mockup), on
// the existing vault palette tokens (there is no dedicated "success green" token: OK reuses
// the neutral steel-light, Warning/Critical reuse the amber/danger tokens already used
// elsewhere on this dashboard, Off here reuses the muted steel-dark).

import type { FeatureStatusKey } from "@/lib/adminTriage";

// T13.8.8 (DA-13-G2-4): "crit" uses --danger-text for the TEXT/icon colour (>= 4.5:1 on the
// dark background, computed ~6.4:1) and keeps --danger only for the border -- see
// globals.css's own comment for why the two tokens differ.
const TONE_CLASS: Record<FeatureStatusKey, string> = {
  ok: "border-vault-steel-light text-vault-steel-light",
  warn: "border-vault-amber text-vault-amber",
  crit: "border-vault-danger text-vault-danger-text",
  off: "border-vault-steel-dark text-vault-steel",
};

function Icon({ tone }: { tone: FeatureStatusKey }) {
  const common = {
    width: 12,
    height: 12,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2.4,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  if (tone === "ok") {
    return (
      <svg {...common}>
        <path d="M5 12.5l4.5 4.5L19 7.5" />
      </svg>
    );
  }
  if (tone === "warn" || tone === "crit") {
    return (
      <svg {...common}>
        <path d="M12 4l9 16H3z" />
        <path d="M12 10v4M12 17.5v.01" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8 12h8" />
    </svg>
  );
}

export default function StatusBadge({
  tone,
  label,
  testId,
}: {
  tone: FeatureStatusKey;
  label: string;
  testId?: string;
}) {
  return (
    <span
      data-testid={testId}
      className={`inline-flex min-w-[6.5rem] flex-shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold ${TONE_CLASS[tone]}`}
    >
      <Icon tone={tone} />
      {label}
    </span>
  );
}
