// What every set-up tab shares: a draft of some profile sections, saved into the right file.
import { useEffect, useRef, useState } from "react";
import { useToast } from "../../components/feedback";
import type { Obj } from "../../components/form";
import { setLeaveGuard } from "../../lib/router";
import { setupApi, type ProfileDetail } from "../../lib/setup";

export interface TabProps { detail: ProfileDetail; reload: () => void }

// A draft of some sections. save() writes each section into `file` and reports problems.
export function useDraft<T extends Obj>(initial: T, file: string, project: string, reload: () => void) {
  const toast = useToast();
  const [draft, setDraft] = useState<T>(initial);
  const [base, setBase] = useState(JSON.stringify(initial));
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [version, setVersion] = useState(0);   // bumps when the draft is reset, so uncontrolled lists re-read it
  const dirty = JSON.stringify(draft) !== base;
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useEffect(() => {
    setLeaveGuard(() => !dirtyRef.current || confirm("You have unsaved changes. Leave without saving?"));
    const onUnload = (e: BeforeUnloadEvent) => { if (dirtyRef.current) e.preventDefault(); };
    window.addEventListener("beforeunload", onUnload);
    return () => { setLeaveGuard(null); window.removeEventListener("beforeunload", onUnload); };
  }, []);

  const save = async (sections?: Obj) => {
    setBusy(true);
    try {
      const r = await setupApi.saveProfile(project, file, sections ?? draft);
      if (!r.saved) {
        setProblems(r.problems);
        toast("Not saved: see the problems below", "bad");
        return false;
      }
      setProblems([]);
      setBase(JSON.stringify(draft));
      toast("Saved");
      reload();
      return true;
    } catch (e) {
      toast((e as Error).message, "bad");
      return false;
    } finally {
      setBusy(false);
    }
  };
  const revert = () => { setDraft(JSON.parse(base)); setProblems([]); setVersion((v) => v + 1); };
  return { draft, setDraft, dirty, busy, save, revert, problems, version };
}

export const asObj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
export const asList = <T = Obj,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
export const str = (v: unknown) => (v == null ? "" : String(v));
