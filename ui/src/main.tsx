import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

// Text cut short with "…" can always be read in full: hovering a truncated line shows the whole of it.
document.addEventListener("pointerover", (e) => {
  const el = (e.target as HTMLElement | null)?.closest?.<HTMLElement>(".truncate");
  if (el && !el.title && el.scrollWidth > el.clientWidth) el.title = (el.textContent || "").trim();
}, { passive: true });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
