"use client";

import { useEffect, useRef } from "react";

import { measureCellAspect } from "@/lib/vaultGeometry";
import {
  STICKMAN_FRAME_SEQUENCE,
  STICKMAN_FRAME_INTERVAL_MS,
  buildAjarGrid,
  gridToHtml,
  nextFrameIndex,
  shouldAnimate,
} from "@/lib/vaultAjarScene";

/* ============================================================================
   NotFoundVault — the 404 page's ASCII illustration: the vault door standing
   ajar (components/VaultDoor.tsx's own geometry, lib/vaultGeometry.ts, drawn
   open rather than closed) and a confused stickman who has walked out of it
   and stopped well clear of the frame.

   Same pattern as VaultDoor.tsx: the grid itself is pure (lib/vaultAjarScene.ts,
   no DOM), only the font measurement and the redraw-on-resize/interval are done
   here, imperatively, against a ref -- no dangerouslySetInnerHTML, no inline
   <script> (CSP: no new script-src/style-src exception needed, same as
   VaultDoor.tsx already proves under the `security-headers` gate step).

   Respects prefers-reduced-motion: draws frame 0 once and never starts the
   interval, so the art never moves for a visitor who asked for no motion.
   ========================================================================== */
export default function NotFoundVault() {
  const preRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const pre = preRef.current;
    if (!pre) return;

    let aspect = 0.55;
    const remeasure = () => {
      aspect = measureCellAspect(pre);
    };
    const draw = (frame: number) => {
      pre.innerHTML = gridToHtml(buildAjarGrid(frame, aspect));
    };

    remeasure();
    draw(STICKMAN_FRAME_SEQUENCE[0]);

    const onResize = () => {
      remeasure();
      draw(STICKMAN_FRAME_SEQUENCE[0]);
    };
    window.addEventListener("resize", onResize);

    const reducedMotion =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let intervalId: ReturnType<typeof setInterval> | undefined;
    if (shouldAnimate(reducedMotion)) {
      let sequenceIndex = 0;
      intervalId = setInterval(() => {
        const next = nextFrameIndex(sequenceIndex);
        sequenceIndex = next.sequenceIndex;
        draw(next.frame);
      }, STICKMAN_FRAME_INTERVAL_MS);
    }

    return () => {
      window.removeEventListener("resize", onResize);
      if (intervalId !== undefined) clearInterval(intervalId);
    };
  }, []);

  return (
    <div className="flex w-full justify-center overflow-hidden">
      <pre
        ref={preRef}
        aria-hidden="true"
        className="m-0 select-none whitespace-pre text-vault-steel"
        style={{
          fontSize: "clamp(4.5px, 1.25vw, 10.5px)",
          lineHeight: 1.02,
          letterSpacing: 0,
        }}
      />
    </div>
  );
}
