// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import type { Deck } from "@deck/sdk";
import { readChunked, writeChunked } from "./chunked-storage.ts";
import { stateLabel, type Draft, type Evidence } from "./logic.ts";
import { general, lunch, type Recipe } from "./workflows.ts";
export interface State {
  version: 1;
  drafts: Draft[];
  recipes: Recipe[];
}
export const STATE_KEY = "coolreserve.state";
export const initialState = (): State => ({
  version: 1,
  drafts: [],
  recipes: [structuredClone(general), structuredClone(lunch)],
});
function invalid(): never {
  throw new Error("저장한 초안 형식이 손상되었습니다. 백업 파일로 복구하거나 새 작업을 시작하세요.");
}
function record(x: unknown): x is Record<string, unknown> {
  return !!x && typeof x === "object" && !Array.isArray(x);
}
function text(x: unknown, max = 30000): x is string {
  return typeof x === "string" && x["length"] <= max;
}
function strings(x: unknown, max: number): x is string[] {
  return Array.isArray(x) && x["length"] <= max && x["every"]((v) => text(v));
}
function validEvidence(x: unknown): x is Evidence {
  return record(x) && text(x["displayName"], 1024) && text(x["department"], 1024) && text(x["accountLabel"], 1024);
}
function bindings(x: unknown): boolean {
  return record(x) && Object.keys(x).length <= 100 && Object.values(x).every(validEvidence);
}
export function validateState(value: unknown): State {
  if (
    !record(value) ||
    value["version"] !== 1 ||
    !Array.isArray(value["drafts"]) ||
    !Array.isArray(value["recipes"]) ||
    value["recipes"].length > 30 ||
    value["drafts"].length > 2500
  )
    invalid();
  const ids = new Set<string>();
  for (const d of value["drafts"]) {
    if (
      !record(d) ||
      !text(d["id"], 160) ||
      ids.has(d["id"]) ||
      !strings(d["recipientNames"], 100) ||
      !bindings(d["bindings"]) ||
      !text(d["title"], 400) ||
      !text(d["body"], 60000) ||
      !text(d["scheduledAt"], 16) ||
      typeof d["retryLocked"] !== "boolean" ||
      typeof d["archived"] !== "boolean" ||
      !Number.isInteger(d["revision"]) ||
      Number(d["revision"]) < 1 ||
      Number(d["revision"]) > 4294967295 ||
      typeof d["state"] !== "string" ||
      !Object.hasOwn(stateLabel, d["state"]) ||
      !Array.isArray(d["attachments"]) ||
      d["attachments"].length !== 0 ||
      !strings(d["missingAttachments"], 10)
    )
      invalid();
    if (
      d["workflow"] !== undefined &&
      (!record(d["workflow"]) ||
        !text(d["workflow"]["family"], 200) ||
        !text(d["workflow"]["source"], 1024) ||
        !text(d["workflow"]["key"], 64) ||
        !text(d["workflow"]["day"], 10) ||
        !strings(d["workflow"]["occurrences"], 1000) ||
        !text(d["workflow"]["baseline"], 150000))
    )
      invalid();
    ids.add(d["id"]);
  }
  if (value["drafts"].filter((d) => !(d as Draft).archived).length > 500) invalid();
  const recipeIds = new Set<string>();
  for (const r of value["recipes"]) {
    if (
      !record(r) ||
      !text(r["id"], 160) ||
      recipeIds.has(r["id"]) ||
      !text(r["name"], 200) ||
      !["general", "lunch"].includes(String(r["preset"])) ||
      !["row", "date"].includes(String(r["grouping"])) ||
      !Number.isInteger(r["headerRows"]) ||
      Number(r["headerRows"]) < 1 ||
      Number(r["headerRows"]) > 10 ||
      !record(r["columns"]) ||
      Object.values(r["columns"]).some((x) => !text(x, 1024)) ||
      !Number.isInteger(r["offsetDays"]) ||
      Math.abs(Number(r["offsetDays"])) > 90 ||
      !text(r["time"], 5) ||
      !text(r["title"], 400) ||
      !text(r["body"], 60000) ||
      !bindings(r["mapping"]) ||
      !Array.isArray(r["slots"]) ||
      r["slots"].length > 2 ||
      r["slots"].some((s) => !record(s) || !text(s["id"], 80) || !text(s["label"], 80) || !text(s["time"], 80))
    )
      invalid();
    recipeIds.add(r["id"]);
  }
  return value as unknown as State;
}
function stored(state: State): State {
  return {
    ...state,
    drafts: state.drafts.map((d) => ({
      ...d,
      attachments: [],
      missingAttachments: [...d["missingAttachments"], ...d["attachments"].map((f) => f.name)],
    })),
  };
}
export async function loadState(deck: Deck): Promise<State> {
  const value = await readChunked(deck.storage, STATE_KEY);
  if (value === null) return initialState();
  const state = validateState(value);
  return {
    ...state,
    drafts: state.drafts.map((d) =>
      ["submitting", "preparing"].includes(d["state"])
        ? {
            ...d,
            state: d["state"] === "submitting" ? "uncertain" : "failed",
            retryLocked: d["state"] === "submitting" || d["retryLocked"],
          }
        : d,
    ),
  };
}
export async function saveState(deck: Deck, state: State, recover = false): Promise<void> {
  const value = validateState(stored(state));
  await writeChunked(deck.storage, STATE_KEY, value, { recover, validatePrevious: validateState });
}
export function backup(state: State): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(stored(state)));
}
export function readBackup(bytes: Uint8Array): State {
  if (bytes.length > 2 * 1024 * 1024) throw new Error("백업 파일은 2MiB 이내여야 합니다.");
  try {
    return validateState(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)));
  } catch {
    invalid();
  }
}
