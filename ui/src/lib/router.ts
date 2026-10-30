// Hash routing (#/p/clinic/runs/r-...): the dashboard is one page served by the Python server, so the
// address after # is the page. A page with unsaved edits can block leaving.
import { useEffect, useState } from "react";

export interface Location { path: string; query: URLSearchParams; hash: string }

function read(): Location {
  const hash = location.hash || "#/";
  const [path, query] = (hash.slice(1) || "/").split("?");
  return { path: path || "/", query: new URLSearchParams(query || ""), hash };
}

type Blocker = () => boolean;   // returns true when leaving is fine
let blocker: Blocker | null = null;
let current = location.hash || "#/";

export function setLeaveGuard(fn: Blocker | null) {
  blocker = fn;
}

export function useHashLocation(): Location {
  const [loc, setLoc] = useState(read);
  useEffect(() => {
    const onChange = () => {
      if (blocker && location.hash !== current && !blocker()) {
        history.replaceState(null, "", current);
        return;
      }
      current = location.hash || "#/";
      setLoc(read());
    };
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return loc;
}

// replace: swap the current history entry (a redirect), so Back doesn't land on it again.
export const navigate = (hash: string, replace = false) => {
  if (replace) location.replace(location.pathname + location.search + hash);
  else location.hash = hash;
};
