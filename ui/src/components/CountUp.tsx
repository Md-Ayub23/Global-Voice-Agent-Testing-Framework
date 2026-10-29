import { useEffect, useState } from "react";

// A number that counts up when it first appears (instant with reduced motion).
export function CountUp({ to, decimals = 0, suffix = "" }: { to: number | null | undefined; decimals?: number; suffix?: string }) {
  const [v, setV] = useState(0);
  useEffect(() => {
    if (to == null || Number.isNaN(to)) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const start = performance.now(), dur = reduce ? 0 : 700;
    let raf = 0;
    const step = (now: number) => {
      const k = dur ? Math.min(1, (now - start) / dur) : 1;
      setV(to * (1 - Math.pow(1 - k, 3)));
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [to]);
  if (to == null || Number.isNaN(to)) return <>—</>;
  return <>{v.toFixed(decimals)}{suffix}</>;
}
