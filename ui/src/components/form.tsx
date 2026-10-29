// Small form controls for the project set-up pages: text, numbers, pick lists, key/value tables and
// lists of strings, plus the save bar and the result box of a "Test" button.
import { useState, type ReactNode } from "react";
import { cx } from "../lib/format";
import { Icon } from "./Icon";
import { Button, Chip, Notice, inputCls } from "./ui";

export type Json = string | number | boolean | null | Json[] | { [k: string]: Json };
export type Obj = { [k: string]: Json };
export type Patch = { [k: string]: Json | undefined };

export function Text({ value, onChange, placeholder, mono, id, type = "text", className, disabled }: {
  value: string | undefined | null; onChange: (v: string) => void; placeholder?: string; mono?: boolean; id?: string;
  type?: string; className?: string; disabled?: boolean;
}) {
  return (
    <input id={id} type={type} className={cx(inputCls, mono && "font-mono text-[12.5px]", "font-normal", className)} value={value ?? ""}
      placeholder={placeholder} disabled={disabled} autoComplete="off" spellCheck={false} onChange={(e) => onChange(e.target.value)} />
  );
}

export function Num({ value, onChange, placeholder, step, min, max, id }: {
  value: number | undefined | null; onChange: (v: number | undefined) => void; placeholder?: string | number; step?: number;
  min?: number; max?: number; id?: string;
}) {
  return (
    <input id={id} type="number" className={cx(inputCls, "font-normal")} value={value ?? ""} step={step} min={min} max={max}
      placeholder={placeholder == null ? "" : String(placeholder)}
      onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))} />
  );
}

export function Select({ value, options, onChange, id, className, placeholder }: {
  value: string | undefined | null; options: (string | [string, string])[]; onChange: (v: string) => void; id?: string;
  className?: string; placeholder?: string;
}) {
  const opts = options.map((o) => (typeof o === "string" ? [o, o] : o));
  const known = opts.some(([v]) => v === (value ?? ""));
  return (
    <select id={id} className={cx(inputCls, "font-normal", className)} value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
      {placeholder != null && <option value="">{placeholder}</option>}
      {!known && value ? <option value={value}>{value}</option> : null}
      {opts.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
    </select>
  );
}

export function Area({ value, onChange, rows = 3, mono, placeholder, id }: {
  value: string | undefined | null; onChange: (v: string) => void; rows?: number; mono?: boolean; placeholder?: string; id?: string;
}) {
  return (
    <textarea id={id} rows={rows} spellCheck={!mono} placeholder={placeholder} value={value ?? ""} onChange={(e) => onChange(e.target.value)}
      className={cx(inputCls, "h-auto py-2 leading-5 font-normal resize-y", mono && "font-mono text-[12.5px]")} />
  );
}

export function Check({ checked, onChange, label, hint }: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode; hint?: ReactNode }) {
  return (
    <label className="flex items-start gap-2 cursor-pointer select-none text-[13px]">
      <input type="checkbox" className="mt-[3px] accent-[var(--accent)]" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span><span className="font-semibold">{label}</span>{hint && <span className="block text-muted text-xs">{hint}</span>}</span>
    </label>
  );
}

// "a, b, c" <-> ["a", "b", "c"]
export function Csv({ value, onChange, placeholder, id }: { value: string[] | undefined; onChange: (v: string[]) => void; placeholder?: string; id?: string }) {
  const [text, setText] = useState((value || []).join(", "));
  return (
    <input id={id} className={cx(inputCls, "font-normal")} value={text} placeholder={placeholder}
      onChange={(e) => { setText(e.target.value); onChange(e.target.value.split(",").map((s) => s.trim()).filter(Boolean)); }} />
  );
}

const scalar = (v: Json): string => (v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
function parseScalar(s: string, typed: boolean): Json {
  if (!typed) return s;
  if (s === "true") return true;
  if (s === "false") return false;
  if (s !== "" && !Number.isNaN(Number(s)) && !/^0\d/.test(s)) return Number(s);
  return s;
}

// A table of name -> value rows. `typed` turns "3" into 3 and "true" into true (args, expected fields).
export function KeyValue({ value, onChange, keyPh = "name", valPh = "value", typed, id, keyOptions }: {
  value: Obj | undefined | null; onChange: (v: Obj) => void; keyPh?: string; valPh?: string; typed?: boolean; id?: string; keyOptions?: string[];
}) {
  const [rows, setRows] = useState<[string, string][]>(() => Object.entries(value || {}).map(([k, v]) => [k, scalar(v)]));
  const push = (next: [string, string][]) => {
    setRows(next);
    const out: Obj = {};
    for (const [k, v] of next) if (k.trim()) out[k.trim()] = parseScalar(v, !!typed);
    onChange(out);
  };
  const listId = keyOptions?.length ? `${id || "kv"}-keys` : undefined;
  return (
    <div id={id} className="grid gap-1.5">
      {listId && <datalist id={listId}>{keyOptions!.map((k) => <option key={k} value={k} />)}</datalist>}
      {rows.map(([k, v], i) => (
        <div key={i} className="flex gap-1.5 items-center">
          <input className={cx(inputCls, "font-mono text-[12.5px] font-normal w-[38%]")} placeholder={keyPh} value={k} list={listId}
            onChange={(e) => push(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          <input className={cx(inputCls, "font-normal flex-1")} placeholder={valPh} value={v}
            onChange={(e) => push(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
          <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Remove row" onClick={() => push(rows.filter((_, j) => j !== i))} />
        </div>
      ))}
      <div><Button size="sm" icon="plus" onClick={() => setRows([...rows, ["", ""]])}>Add</Button></div>
    </div>
  );
}

export function StrList({ value, onChange, placeholder, mono, id, addLabel = "Add" }: {
  value: string[] | undefined; onChange: (v: string[]) => void; placeholder?: string; mono?: boolean; id?: string; addLabel?: string;
}) {
  const rows = value || [];
  return (
    <div id={id} className="grid gap-1.5">
      {rows.map((v, i) => (
        <div key={i} className="flex gap-1.5 items-center">
          <input className={cx(inputCls, "font-normal flex-1", mono && "font-mono text-[12.5px]")} placeholder={placeholder} value={v}
            onChange={(e) => onChange(rows.map((r, j) => (j === i ? e.target.value : r)))} />
          <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Remove" onClick={() => onChange(rows.filter((_, j) => j !== i))} />
        </div>
      ))}
      <div><Button size="sm" icon="plus" onClick={() => onChange([...rows, ""])}>{addLabel}</Button></div>
    </div>
  );
}

export function Help({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cx("text-muted text-[12.5px] leading-5", className)}>{children}</p>;
}

export function Grid({ children, cols = 2, className }: { children: ReactNode; cols?: 2 | 3 | 4; className?: string }) {
  const c = { 2: "sm:grid-cols-2", 3: "sm:grid-cols-3", 4: "sm:grid-cols-2 lg:grid-cols-4" }[cols];
  return <div className={cx("grid gap-3 grid-cols-1", c, className)}>{children}</div>;
}

export function Problems({ items, title = "Problems to fix:" }: { items: string[]; title?: string }) {
  if (!items.length) return null;
  return (
    <Notice tone="warn">
      <b>{title}</b>
      <ul className="mt-1.5 pl-[18px] list-disc">{items.map((p, i) => <li key={i} className="font-mono text-[12px] break-words">{p}</li>)}</ul>
    </Notice>
  );
}

export function SaveBar({ dirty, busy, onSave, onRevert, note, id = "save-section", revertLabel }: {
  dirty: boolean; busy?: boolean; onSave: () => void; onRevert?: () => void; note?: ReactNode; id?: string; revertLabel?: string;
}) {
  return (
    <div className="sticky bottom-3 z-3 mt-4 flex items-center gap-2.5 flex-wrap px-3.5 py-2.5 rounded-xl border border-line bg-[color-mix(in_srgb,var(--surface)_92%,transparent)] backdrop-blur shadow-soft">
      {dirty ? <Chip tone="warn">unsaved changes</Chip> : <Chip tone="good">saved</Chip>}
      <span className="text-muted text-xs flex-1 min-w-0">{note}</span>
      {onRevert && <Button size="sm" disabled={(!dirty && !revertLabel) || busy} onClick={onRevert}>{revertLabel || "Undo changes"}</Button>}
      <Button id={id} size="sm" variant="primary" icon="save" busy={busy} disabled={!dirty || busy} onClick={onSave}>Save</Button>
    </div>
  );
}

export interface TestOutcome {
  ok: boolean;
  message: string;
  details?: string[];
  rows?: Record<string, Json>[];
  lines?: string[];
  value?: Json;
}

export function TestResult({ result }: { result: TestOutcome | null }) {
  if (!result) return null;
  const cols = result.rows?.length ? Object.keys(result.rows[0]) : [];
  return (
    <Notice tone={result.ok ? "good" : "bad"} className="mt-3" icon={result.ok ? "check" : "alert"}>
      <div data-test-result={result.ok ? "ok" : "bad"} className="font-semibold">{result.message}</div>
      {result.details?.map((d, i) => <div key={i} className="text-[12.5px] mt-0.5 break-words">{d}</div>)}
      {cols.length > 0 && (
        <div className="overflow-x-auto mt-2">
          <table className="text-[12px] font-mono border-collapse">
            <thead><tr>{cols.map((c) => <th key={c} className="text-left pr-3 pb-1 text-muted font-semibold">{c}</th>)}</tr></thead>
            <tbody>{result.rows!.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c} className="pr-3 py-0.5 whitespace-nowrap">{scalar(r[c] ?? null)}</td>)}</tr>)}</tbody>
          </table>
        </div>
      )}
      {result.lines && result.lines.length > 0 && (
        <pre className="mt-2 max-h-[260px] overflow-auto text-[11.5px] font-mono whitespace-pre-wrap break-words bg-surface/60 rounded-lg p-2">{result.lines.join("\n")}</pre>
      )}
    </Notice>
  );
}

// A titled block inside a card.
export function Block({ title, sub, children, icon, aside }: { title: ReactNode; sub?: ReactNode; children: ReactNode; icon?: Parameters<typeof Icon>[0]["name"]; aside?: ReactNode }) {
  return (
    <section className="grid gap-3 py-4 first:pt-0 border-b border-line last:border-b-0">
      <div className="flex items-start gap-2 flex-wrap">
        {icon && <Icon name={icon} className="mt-0.5 text-muted" />}
        <div className="flex-1 min-w-0">
          <h3 className="font-semibold">{title}</h3>
          {sub && <div className="text-muted text-[12.5px] mt-0.5">{sub}</div>}
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}

// Immutable update of a nested value: setIn(doc, ["caller", "facts"], {...}). undefined/"" removes the key.
export function setIn(doc: Obj, path: (string | number)[], value: Json | undefined): Obj {
  const [head, ...rest] = path;
  const copy: Obj = Array.isArray(doc) ? ([...(doc as Json[])] as unknown as Obj) : { ...(doc || {}) };
  if (!rest.length) {
    if (value === undefined || value === "") delete copy[head as string];
    else copy[head as string] = value;
    return copy;
  }
  copy[head as string] = setIn((copy[head as string] as Obj) || {}, rest, value) as Json;
  return copy;
}

export function getIn(doc: Json | undefined, path: (string | number)[]): Json | undefined {
  let cur: Json | undefined = doc;
  for (const p of path) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, Json>)[p as string];
  }
  return cur;
}
