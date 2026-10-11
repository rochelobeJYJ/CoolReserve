// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import { describe, expect, it } from "vitest";
import { evidence } from "./logic.ts";
import { applyWorkflow, detectHeader, general, lunch, render, workflowReview } from "./workflows.ts";
const now = new Date("2026-10-11T00:00:00Z");
const accounts = [
  { recipientHandle: "r1", displayName: "가상교사가", department: "합성부서1", accountLabel: "계정1" },
  { recipientHandle: "r2", displayName: "가상교사나", department: "합성부서2", accountLabel: "계정2" },
];
const mapping = Object.fromEntries(accounts.map((a) => [a.displayName, evidence(a)]));
const values = [
  ["업무일", "담당자", "제목", "내용"],
  ["2026-10-12", "가상교사가", "", "합성 안내"],
];
const cfg = () => ({ ...structuredClone(general), mapping });
describe("source-driven workflows", () => {
  it("defaults to general and preserves optional title and a stable row across reordering", async () => {
    const first = await workflowReview(values, cfg(), "2026-10-12", "2026-11-01", "합성자료", accounts, [], now);
    expect(first.candidates[0]?.draft.title).toBe("");
    expect(first.candidates[0]?.issues).toEqual([]);
    const saved = applyWorkflow(first, []);
    const again = await workflowReview(values, cfg(), "2026-10-12", "2026-11-01", "합성자료", accounts, saved, now);
    expect(again.candidates[0]?.draft.id).toBe(saved[0]?.id);
    expect(again.candidates[0]?.change).toBe("unchanged");
  });
  it("keeps individual edits if source is unchanged, blocks overlap and deletes only untouched unsubmitted rows", async () => {
    const recipe = cfg();
    recipe.columns["id"] = "업무ID";
    const rows = values.map((r, i) => [...r, i ? "합성업무1" : "업무ID"]);
    const first = await workflowReview(rows, recipe, "2026-10-12", "2026-11-01", "합성자료", accounts, [], now);
    const saved = applyWorkflow(first, []).map((d) => ({ ...d, body: "개별 수정" }));
    const unchanged = await workflowReview(rows, recipe, "2026-10-12", "2026-11-01", "합성자료", accounts, saved, now);
    expect(unchanged.candidates[0]?.draft.body).toBe("개별 수정");
    const changed = structuredClone(rows);
    if (changed[1]) changed[1][3] = "원본 변경";
    const overlap = await workflowReview(changed, recipe, "2026-10-12", "2026-11-01", "합성자료", accounts, saved, now);
    expect(overlap.candidates[0]?.issues.join(" ")).toContain("개별 수정");
    expect(() => applyWorkflow(overlap, saved)).toThrow();
    const removed = await workflowReview(
      [rows[0] ?? []],
      recipe,
      "2026-10-12",
      "2026-11-01",
      "합성자료",
      accounts,
      saved,
      now,
    );
    expect(removed.issues.join(" ")).toContain("개별 수정");
  });
  it("preserves both lunch slots, removes explicit exclusion and skips second slot only on first-only days", async () => {
    const recipe = { ...structuredClone(lunch), mapping };
    const rows = [
      ["날짜", "1차 담당자", "2차 담당자", "제외일", "고사 1차만"],
      ["2026-10-12", "가상교사가", "가상교사나", "FALSE", "FALSE"],
      ["2026-10-13", "가상교사가", "", "FALSE", "TRUE"],
      ["2026-10-14", "", "", "TRUE", "FALSE"],
    ];
    const result = await workflowReview(rows, recipe, "2026-10-12", "2026-10-14", "합성배정", accounts, [], now);
    expect(result.occurrences).toBe(3);
    expect(result.excluded).toBe(1);
    expect(result.candidates).toHaveLength(2);
    expect(result.candidates[0]?.draft.recipientNames).toHaveLength(2);
    expect(result.candidates[0]?.draft.body).toContain("2차: 12:40~13:00");
    expect(result.candidates[1]?.draft.body).not.toContain("2차:");
  });
  it("requires explicit source-name mapping and blocks changed attempted occurrences or source scope", async () => {
    const unmatched = await workflowReview(values, general, "2026-10-12", "2026-11-01", "합성자료", accounts, [], now);
    expect(unmatched.unmatched).toEqual(["가상교사가"]);
    const first = await workflowReview(values, cfg(), "2026-10-12", "2026-11-01", "합성자료", accounts, [], now);
    const saved = applyWorkflow(first, []).map((d) => ({ ...d, retryLocked: true }));
    const scope = await workflowReview(values, cfg(), "2026-10-12", "2026-11-01", "다른자료", accounts, saved, now);
    expect(scope.issues.length).toBeGreaterThan(0);
    const removed = await workflowReview(
      [values[0] ?? []],
      cfg(),
      "2026-10-12",
      "2026-11-01",
      "합성자료",
      accounts,
      saved,
      now,
    );
    expect(removed.issues.join(" ")).toContain("등록 시도");
  });
  it("detects source headers and rejects unsupported template code, duplicate tasks and invalid booleans", async () => {
    expect(detectHeader([["자료 제목"], ...values], cfg())).toBe(2);
    expect(render("{{원문}} {내용}", { 내용: "합성" })).toBe("{원문} 합성");
    expect(() => render("{내용.__class__}", { 내용: "합성" })).toThrow();
    const duplicate = await workflowReview(
      [...values, values[1] ?? []],
      cfg(),
      "2026-10-12",
      "2026-11-01",
      "합성",
      accounts,
      [],
      now,
    );
    expect(duplicate.issues.join(" ")).toContain("중복");
  });
});
