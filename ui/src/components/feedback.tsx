// Toasts and hover tooltips, shared by every page.
import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cx } from "../lib/format";

// ---- toasts ---------------------------------------------------------------------------------

type ToastKind = "" | "bad";
type ToastFn = (message: string, kind?: ToastKind) => void;
const ToastCtx = createContext<ToastFn>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<{ id: number; message: string; kind: ToastKind; leaving: boolean }[]>([]);
  const next = useRef(0);
  const toast = useCallback<ToastFn>((message, kind = "") => {
    const id = ++next.current;
    const life = kind === "bad" ? 6000 : 3200;
    setItems((xs) => [...xs, { id, message, kind, leaving: false }]);
    setTimeout(() => setItems((xs) => xs.map((x) => (x.id === id ? { ...x, leaving: true } : x))), life);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), life + 350);
  }, []);
  return (
    <ToastCtx.Provider value={toast}>
      {children}
      {createPortal(
        <div className="fixed right-5 bottom-5 grid gap-2 z-60" role="status" aria-live="polite">
          {items.map((t) => (
            <div
              key={t.id}
              className={cx(
                "px-3.5 py-2.5 rounded-[11px] shadow-pop font-medium text-[13px] max-w-[380px] animate-rise transition-opacity duration-300",
                t.kind === "bad" ? "bg-bad text-white" : "bg-ink text-bg",
                t.leaving && "opacity-0",
              )}
            >
              {t.message}
            </div>
          ))}
        </div>,
        document.body,
      )}
    </ToastCtx.Provider>
  );
}

export function useCopy() {
  const toast = useToast();
  return useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied");
    } catch {
      toast("Couldn't copy; select the text instead", "bad");
    }
  }, [toast]);
}

// ---- tooltips -------------------------------------------------------------------------------

interface TipState { content: ReactNode; x: number; y: number }
const TipCtx = createContext<(t: TipState | null) => void>(() => {});

export function TooltipProvider({ children }: { children: ReactNode }) {
  const [tip, setTip] = useState<TipState | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: 0, top: 0 });
  useLayoutEffect(() => {
    if (!tip || !ref.current) return;
    const r = ref.current.getBoundingClientRect();
    const left = Math.min(window.innerWidth - r.width - 12, tip.x + 14);
    const top = tip.y + r.height + 24 > window.innerHeight ? tip.y - r.height - 12 : tip.y + 16;
    setPos({ left: Math.max(8, left), top: Math.max(8, top) });
  }, [tip]);
  return (
    <TipCtx.Provider value={setTip}>
      {children}
      {createPortal(
        <div
          ref={ref}
          role="tooltip"
          style={pos}
          className={cx(
            "fixed z-50 pointer-events-none bg-surface text-ink border border-line shadow-pop rounded-[10px] px-2.5 py-2 text-[12.5px] max-w-[300px]",
            "transition-[opacity,transform] duration-100",
            tip ? "opacity-100 translate-y-0" : "opacity-0 translate-y-1",
          )}
        >
          {tip?.content}
        </div>,
        document.body,
      )}
    </TipCtx.Provider>
  );
}

// Spread the result on any element (HTML or SVG) to show `content` while hovering it.
export function useTip(content: ReactNode | null | undefined) {
  const set = useContext(TipCtx);
  return useMemo(() => {
    if (content == null || content === "") return {};
    const show = (e: React.MouseEvent) => set({ content, x: e.clientX, y: e.clientY });
    return { onMouseEnter: show, onMouseMove: show, onMouseLeave: () => set(null) };
  }, [content, set]);
}

export function Tip({ content, children, className }: { content: ReactNode; children: ReactNode; className?: string }) {
  const handlers = useTip(content);
  return (
    <span className={className} {...handlers}>
      {children}
    </span>
  );
}

// The same, for SVG marks in charts.
export function TipG({ content, children, className }: { content: ReactNode; children: ReactNode; className?: string }) {
  const handlers = useTip(content);
  return (
    <g className={className} {...handlers}>
      {children}
    </g>
  );
}
