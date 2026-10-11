// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import { describe, expect, it } from "vitest";
import {
  batchDrafts,
  createDraft,
  day,
  evidence,
  parseTable,
  repeat,
  resolveNames,
  scheduledMessage,
  validateDraft,
} from "./logic.ts";
const now = new Date("2026-10-11T00:00:00Z");
const accounts = [
  { recipientHandle: "r1", displayName: "가상교사가", department: "합성부서1", accountLabel: "계정1" },
  { recipientHandle: "r2", displayName: "가상교사가", department: "합성부서2", accountLabel: "계정2" },
];
const first = accounts[0];
if (!first) throw new Error("TEST_SETUP");
const valid = () =>
  createDraft({
    recipientNames: ["가상교사가"],
    bindings: { 가상교사가: evidence(first) },
    body: "합성 본문",
    scheduledAt: "2026-10-12T09:30",
  });
describe("original reservation input", () => {
  it("reads optional title, Excel fractional time and quoted multiline CSV without changing body", () => {
    const rows = parseTable('받는사람,예약날짜,예약시간,내용\n가상교사가,2026-10-12,0.5,"첫 줄, 쉼표\n둘째 줄"');
    const draft = batchDrafts(rows)[0];
    expect(draft?.title).toBe("");
    expect(draft?.body).toBe("첫 줄, 쉼표\n둘째 줄");
    expect(draft?.scheduledAt).toBe("2026-10-12T12:00");
    expect(() => parseTable('a,"b"oops')).toThrow();
    expect(() =>
      batchDrafts([
        ["받는사람", "내용"],
        ["가상교사가", "본문"],
      ]),
    ).toThrow();
  });
  it("requires a choice for identical names and exact evidence after reconnect", () => {
    expect(resolveNames(["가상교사가"], {}, accounts).issues).toHaveLength(1);
    const bindings = { 가상교사가: evidence(first) };
    expect(
      resolveNames(
        ["가상교사가"],
        bindings,
        accounts.map((a) => ({ ...a, recipientHandle: a.recipientHandle + "-new" })),
      ).handles,
    ).toEqual(["r1-new"]);
    expect(
      resolveNames(
        ["가상교사가"],
        bindings,
        accounts.map((a) => ({ ...a, department: "변경부서" })),
      ).issues,
    ).toHaveLength(1);
    expect(
      resolveNames(["별칭1", "별칭2"], { 별칭1: bindings["가상교사가"], 별칭2: bindings["가상교사가"] }, accounts, true)
        .issues,
    ).toHaveLength(1);
  });
  it("expands individual weekly drafts and explicit exclusions without removing holidays", () => {
    const base = valid();
    const drafts = repeat(base, "2026-10-26", [0], ["2026-10-19"], now);
    expect(drafts.map((d) => d.scheduledAt)).toEqual(["2026-10-12T09:30", "2026-10-26T09:30"]);
    expect(drafts[0]?.id).not.toBe(base.id);
    expect(() => repeat(base, "2027-02-01", [0], [], now)).toThrow();
    expect(() => repeat(base, "2026-10-13", [], [], now)).toThrow();
  });
  it("checks calendar dates, ten minute boundary and calendar months", () => {
    expect(() => day("2026-02-30")).toThrow();
    expect(day("2026/10/12")).toBe("2026-10-12");
    expect(validateDraft(valid(), now)).toEqual([]);
    expect(validateDraft({ ...valid(), scheduledAt: "2026-10-11T09:09" }, now)).toContain(
      "예약시간은 지금부터 10분 이상 뒤로 지정하세요.",
    );
    expect(validateDraft({ ...valid(), body: "탭\t본문" }, now).length).toBeGreaterThan(0);
  });
  it("leaves imported paths unopenable and blocks an existing submission lock", () => {
    const draft = batchDrafts([
      ["받는사람", "예약일시", "내용", "첨부파일"],
      ["가상교사가", "2026-10-12 12:00", "본문", "C:\\synthetic\\attachment.txt"],
    ])[0];
    expect(draft?.attachments).toEqual([]);
    expect(draft?.missingAttachments).toEqual(["예약표의 첨부파일"]);
    const message = scheduledMessage({ ...valid(), retryLocked: true }, accounts);
    expect(message.message.title).toBeNull();
    expect(message.issues).toContain("등록 시도 기록이 있어 다시 실행할 수 없습니다.");
  });
  it("reads 500 rows atomically and rejects 501", () => {
    const headers = ["받는사람", "예약날짜", "예약시간", "내용"],
      rows = Array.from({ length: 500 }, (_, n) => ["가상교사가", "2026-10-12", "09:30", `합성${n}`]);
    expect(batchDrafts([headers, ...rows])).toHaveLength(500);
    expect(() => batchDrafts([headers, ...rows, rows[0] ?? []])).toThrow();
  });
});
