// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
// Adapted from CoolReserve app/workflows.py (MIT, Copyright 2026 CoolReserve contributors).
import {
  content,
  createDraft,
  day,
  names,
  norm,
  resolveNames,
  validateDraft,
  type Draft,
  type Evidence,
} from "./logic.ts";
import type { MessagingRecipient } from "@deck/sdk";
export interface Recipe {
  id: string;
  name: string;
  preset: "general" | "lunch";
  headerRows: number;
  grouping: "row" | "date";
  columns: Record<string, string>;
  offsetDays: number;
  time: string;
  title: string;
  body: string;
  slots: { id: string; label: string; time: string }[];
  mapping: Record<string, Evidence>;
}
export const general: Recipe = {
  id: "general",
  name: "일반 업무 안내",
  preset: "general",
  headerRows: 1,
  grouping: "row",
  columns: { date: "업무일", recipient: "담당자", title: "제목", body: "내용", id: "", exclude: "" },
  offsetDays: 0,
  time: "09:05",
  title: "{제목}",
  body: "{내용}",
  slots: [],
  mapping: {},
};
export const lunch: Recipe = {
  id: "lunch",
  name: "중식 지도",
  preset: "lunch",
  headerRows: 1,
  grouping: "date",
  columns: { date: "날짜", first: "1차 담당자", second: "2차 담당자", exclude: "제외일", first_only: "고사 1차만" },
  offsetDays: 0,
  time: "09:05",
  title: "[중식 지도] {지도일}({요일}) 안내",
  body: "안녕하세요, 선생님.\n{지도일}({요일}) 중식 지도 일정을 안내드립니다.\n\n{담당목록}\n\n시간에 맞춰 지도해 주시면 감사하겠습니다.\n오늘도 좋은 하루 보내세요. 감사합니다!",
  slots: [
    { id: "중식1", label: "1차", time: "12:20~12:40" },
    { id: "중식2", label: "2차", time: "12:40~13:00" },
  ],
  mapping: {},
};
export function render(template: string, vars: Record<string, string>): string {
  let result = "";
  for (let i = 0; i < template.length; i++) {
    const c = template[i] ?? "";
    if ((c === "{" || c === "}") && template[i + 1] === c) {
      result += c;
      i++;
    } else if (c === "{") {
      const end = template.indexOf("}", i + 1),
        key = template.slice(i + 1, end);
      if (end < 0 || !Object.hasOwn(vars, key) || /[.[\]:!{}]/.test(key))
        throw new Error("알 수 없거나 지원하지 않는 문구 변수가 있습니다.");
      result += vars[key];
      i = end;
    } else if (c === "}") throw new Error("문구 변수의 중괄호를 확인하세요.");
    else result += c;
  }
  return norm(result);
}
function boolean(value: string): boolean {
  if (["", "FALSE", "false", "0"].includes(norm(value))) return false;
  if (["TRUE", "true", "1"].includes(norm(value))) return true;
  throw new Error("제외일·고사 표시는 TRUE 또는 FALSE여야 합니다.");
}
export function detectHeader(values: string[][], cfg: Recipe): number {
  const required = Object.entries(cfg.columns)
    .filter(([key, column]) => column && (cfg.preset === "lunch" || key !== "title"))
    .map(([, column]) => column);
  const candidates = values.slice(0, 10).flatMap((row, index) => {
    const cells = row.map(norm);
    return !cells.some((x) => !x) &&
      new Set(cells).size === cells.length &&
      required.every((key) => cells.includes(key))
      ? [index + 1]
      : [];
  });
  return candidates.length === 1 ? (candidates[0] ?? cfg.headerRows) : cfg.headerRows;
}
export interface WorkflowReview {
  candidates: { draft: Draft; change: "added" | "changed" | "unchanged"; issues: string[]; sourceRows: number[] }[];
  deleted: Draft[];
  issues: string[];
  unmatched: string[];
  excluded: number;
  occurrences: number;
}
async function hash(text: string): Promise<string> {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))))
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
}
export async function workflowReview(
  values: string[][],
  cfg: Recipe,
  from: string,
  to: string,
  source: string,
  recipients: MessagingRecipient[],
  existing: Draft[],
  now = new Date(),
): Promise<WorkflowReview> {
  const review: WorkflowReview = {
    candidates: [],
    deleted: [],
    issues: [],
    unmatched: [],
    excluded: 0,
    occurrences: 0,
  };
  if (
    !source.trim() ||
    day(from) > day(to) ||
    values.length > 510 ||
    !Number.isInteger(cfg.headerRows) ||
    cfg.headerRows < 1 ||
    cfg.headerRows > 10 ||
    !Number.isInteger(cfg.offsetDays) ||
    Math.abs(cfg.offsetDays) > 90 ||
    !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(cfg.time)
  )
    throw new Error("자료 식별명·기간·열 이름 행·발송시각을 확인하세요.");
  const headers = values[cfg.headerRows - 1]?.map(norm) ?? [];
  if (!headers.length || headers.some((x) => !x) || new Set(headers).size !== headers.length)
    throw new Error("자료의 열 이름이 비었거나 중복되어 있습니다.");
  const required =
    cfg.preset === "lunch" ? ["date", "first", "second", "exclude", "first_only"] : ["date", "recipient"];
  if (
    required.some((key) => !headers.includes(cfg.columns[key] ?? "")) ||
    Object.entries(cfg.columns).some(([key, column]) => key !== "title" && column && !headers.includes(column))
  )
    throw new Error("연결한 열을 원본에서 찾을 수 없습니다.");
  if (
    cfg.preset === "lunch" &&
    (cfg.slots.length !== 2 ||
      cfg.slots.some((x) => !x.id || !x.label || !x.time) ||
      cfg.slots[0]?.id === cfg.slots[1]?.id)
  )
    throw new Error("중식의 1·2차 지도 시간 설정을 확인하세요.");
  let year: number | undefined, month: number | undefined;
  if (cfg.preset === "lunch" && cfg.headerRows > 1) {
    const meta = values[0] ?? [],
      yi = meta.indexOf("연도"),
      mi = meta.indexOf("월");
    if (yi >= 0 || mi >= 0) {
      year = Number(meta[yi + 1]);
      month = Number(meta[mi + 1]);
      day(`${year}-${month}-1`);
    }
  }
  type Record = {
    day: string;
    slot: string;
    label: string;
    time: string;
    names: string[];
    row: { [key: string]: string };
    sourceRow: number;
  };
  const records: Record[] = [],
    groups = new Map<string, Record[]>(),
    seen = new Set<string>();
  for (let i = cfg.headerRows; i < values.length; i++) {
    const cells = values[i] ?? [];
    if (!cells.some(norm)) continue;
    try {
      if (cells.slice(headers.length).some(norm)) throw new Error("열 이름보다 값이 많습니다.");
      const row = Object.fromEntries(headers.map((name, index) => [name, cells[index] ?? ""]));
      const date = day(row[cfg.columns["date"] ?? ""] ?? "", year, month);
      if (year && month && date.slice(0, 7) !== `${year}-${String(month).padStart(2, "0")}`)
        throw new Error("날짜와 첫 행의 연도·월이 다릅니다.");
      if (date < from || date > to) continue;
      if (cfg.columns["exclude"] && boolean(row[cfg.columns["exclude"]] ?? "")) {
        review.excluded++;
        continue;
      }
      if (cfg.preset === "lunch") {
        const only = boolean(row[cfg.columns["first_only"] ?? ""] ?? "");
        for (let s = 0; s < (only ? 1 : 2); s++) {
          const slot = cfg.slots[s];
          if (!slot) throw new Error("지도 차시 설정을 확인하세요.");
          const requested = names(row[cfg.columns[s ? "second" : "first"] ?? ""] ?? "");
          if (requested.length !== 1) throw new Error("한 지도 차시에는 최종 담당자 한 명이 필요합니다.");
          records.push({
            day: date,
            slot: slot.id,
            label: slot.label,
            time: slot.time,
            names: requested,
            row,
            sourceRow: i + 1,
          });
        }
      } else {
        const id = cfg.columns["id"]
          ? norm(row[cfg.columns["id"]])
          : await hash(JSON.stringify(headers.map((h) => [h, row[h]]).sort()));
        if (!id) throw new Error("업무 ID가 비어 있습니다.");
        records.push({
          day: date,
          slot: id,
          label: cfg.columns["id"] ? id : "업무",
          time: "",
          names: names(row[cfg.columns["recipient"] ?? ""] ?? ""),
          row,
          sourceRow: i + 1,
        });
      }
    } catch (error) {
      review.issues.push(`원본 ${i + 1}행: ${error instanceof Error ? error.message : "자료를 확인하세요."}`);
    }
  }
  const family = cfg.preset === "lunch" ? "lunch" : `general:${cfg.id}`;
  for (const r of records) {
    const occurrence = `${family}:${r.day}:${r.slot}`;
    if (seen.has(occurrence)) review.issues.push("같은 날짜·차시의 업무가 중복되어 있습니다.");
    seen.add(occurrence);
    const key = cfg.grouping === "date" ? r.day : `${r.day}:${r.slot}`,
      group = groups.get(key) ?? [];
    group.push(r);
    groups.set(key, group);
  }
  review.occurrences = records.length;
  const old = existing.filter((d) => d.workflow?.family === family && d.workflow.day >= from && d.workflow.day <= to);
  if (old.some((d) => d.workflow?.source !== source))
    review.issues.push("기존 초안과 자료 식별명이 다릅니다. 같은 자료·시트로 확인하세요.");
  const keys = new Set<string>();
  for (const [groupKey, tasks] of groups) {
    const key = await hash(`${family}:${groupKey}`);
    keys.add(key);
    const first = tasks[0];
    if (!first) continue;
    const date = first.day,
      dt = new Date(`${date}T00:00:00Z`),
      previous = old.find((d) => d.workflow?.key === key);
    const requested = [...new Set(tasks.flatMap((t) => t.names))],
      resolved = resolveNames(requested, cfg.mapping, recipients, true),
      issues = [...resolved.issues];
    review.unmatched.push(...requested.filter((n) => !resolved.bindings[n]));
    const vars = {
      ...first.row,
      날짜: date,
      지도일ISO: date,
      지도일: `${dt.getUTCMonth() + 1}월 ${dt.getUTCDate()}일`,
      업무일: `${dt.getUTCMonth() + 1}월 ${dt.getUTCDate()}일`,
      요일: "일월화수목금토"[dt.getUTCDay()] ?? "",
      받는사람: requested.join(" · "),
      담당목록: tasks.map((t) => `${t.label}: ${t.time} ${t.names.join(" · ")} 선생님`).join("\n"),
      제목: first.row[cfg.columns["title"] ?? ""] ?? "",
      내용: first.row[cfg.columns["body"] ?? ""] ?? "",
    };
    dt.setUTCDate(dt.getUTCDate() + cfg.offsetDays);
    const draft = createDraft({
      id: previous?.id ?? crypto.randomUUID(),
      recipientNames: requested,
      bindings: resolved.bindings,
      scheduledAt: `${dt.toISOString().slice(0, 10)}T${cfg.time}`,
      revision: previous ? previous.revision + 1 : 1,
    });
    try {
      draft.title = render(cfg.title, vars);
      draft.body = render(cfg.body, vars);
    } catch (e) {
      issues.push(e instanceof Error ? e.message : "문구 변수를 확인하세요.");
    }
    issues.push(...validateDraft(draft, now));
    const baseline = content(draft),
      change = !previous ? "added" : baseline === previous.workflow?.baseline ? "unchanged" : "changed";
    const occurrences = tasks.map((t) => `${family}:${t.day}:${t.slot}`);
    if (
      old.some(
        (d) =>
          d.retryLocked &&
          d.workflow?.occurrences.some((o) => occurrences.includes(o)) &&
          (d.id !== draft.id || change !== "unchanged"),
      )
    )
      issues.push("이미 등록을 시도한 날짜·차시입니다. 실제 예약을 확인하세요.");
    if (previous && content(previous) !== previous.workflow?.baseline) {
      if (change === "changed") issues.push("개별 수정한 초안과 원본 변경이 겹칩니다. 수동으로 대조하세요.");
      else Object.assign(draft, previous);
    }
    if (previous?.retryLocked) {
      draft.retryLocked = true;
      draft.state = previous.state;
    }
    draft.workflow = { family, source, key, day: date, occurrences, baseline };
    review.candidates.push({ draft, change, issues, sourceRows: [...new Set(tasks.map((t) => t.sourceRow))] });
  }
  review.deleted = old.filter((d) => d.workflow && !keys.has(d.workflow.key));
  if (review.deleted.some((d) => d.retryLocked))
    review.issues.push("원본에서 사라진 업무에 등록 시도가 있습니다. 실제 예약은 자동 취소하지 않습니다.");
  if (review.deleted.some((d) => content(d) !== d.workflow?.baseline))
    review.issues.push("원본에서 사라진 업무에 개별 수정한 초안이 있습니다. 수동으로 대조하세요.");
  review.unmatched = [...new Set(review.unmatched)];
  if (review.candidates.length > 500) throw new Error("만들 메시지가 500건을 넘습니다.");
  return review;
}
export function applyWorkflow(review: WorkflowReview, existing: Draft[]): Draft[] {
  if (review.issues.length || review.candidates.some((c) => c.issues.length))
    throw new Error("모든 자료·수신자 오류를 먼저 확인하세요.");
  const removed = new Set([...review.deleted.map((d) => d.id), ...review.candidates.map((c) => c.draft.id)]);
  return [...existing.filter((d) => !removed.has(d.id)), ...review.candidates.map((c) => c.draft)];
}
