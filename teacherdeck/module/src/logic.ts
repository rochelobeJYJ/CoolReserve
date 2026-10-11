// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
// Adapted from CoolReserve core.py, recipients.py, batch.py (MIT, CoolReserve contributors).
import type { FileHandleInfo, MessagingRecipient, ScheduledMessage, MessagingItemState } from "@deck/sdk";
export interface Evidence {
  displayName: string;
  department: string;
  accountLabel: string;
}
export interface Draft {
  id: string;
  recipientNames: string[];
  bindings: Record<string, Evidence>;
  title: string;
  body: string;
  scheduledAt: string;
  attachments: FileHandleInfo[];
  missingAttachments: string[];
  revision: number;
  state: MessagingItemState;
  retryLocked: boolean;
  archived: boolean;
  workflow?: { family: string; source: string; key: string; day: string; occurrences: string[]; baseline: string };
}
export const norm = (value: unknown): string =>
  String(value ?? "")
    .replaceAll("\r\n", "\n")
    .replaceAll("\r", "\n")
    .trim()
    .normalize("NFC");
export function names(value: string): string[] {
  const result = value
    .split(/[,，;\r\n]/)
    .map(norm)
    .filter(Boolean);
  if (
    !result.length ||
    result.length > 100 ||
    new Set(result).size !== result.length ||
    result.some((x) => x.includes("\t") || x.includes("\0"))
  )
    throw new Error("받는 사람은 중복 없이 1~100명 입력하세요.");
  return result;
}
export const evidence = (r: MessagingRecipient): Evidence => ({
  displayName: r.displayName,
  department: r.department,
  accountLabel: r.accountLabel,
});
export const evidenceKey = (r: Evidence): string => JSON.stringify([r.displayName, r.department, r.accountLabel]);
export const recipientLabel = (r: Evidence): string =>
  `${r.displayName} · ${r.department || "소속 표시 없음"} · ${r.accountLabel}`;
export function candidates(name: string, recipients: MessagingRecipient[]): MessagingRecipient[] {
  return recipients.filter((r) => [r.displayName, r.accountLabel, recipientLabel(r)].map(norm).includes(norm(name)));
}
export function resolveNames(
  requested: string[],
  bindings: Record<string, Evidence>,
  recipients: MessagingRecipient[],
  explicit = false,
): { handles: string[]; bindings: Record<string, Evidence>; issues: string[] } {
  const handles: string[] = [],
    saved: Record<string, Evidence> = {},
    issues: string[] = [];
  for (const name of requested) {
    const chosen = bindings[name];
    const matches = chosen
      ? recipients.filter((r) => evidenceKey(evidence(r)) === evidenceKey(chosen))
      : explicit
        ? []
        : candidates(name, recipients);
    const selected = matches.length === 1 ? matches[0] : undefined;
    if (!selected) {
      issues.push(
        `${name}: ${matches.length > 1 ? "동명이인·중복 계정 표시를 확인하세요." : "실제 계정을 선택하세요."}`,
      );
      continue;
    }
    if (handles.includes(selected.recipientHandle)) {
      issues.push("서로 다른 이름이 같은 계정에 연결되어 있습니다.");
      continue;
    }
    handles.push(selected.recipientHandle);
    saved[name] = evidence(selected);
  }
  return { handles, bindings: saved, issues };
}
export function day(value: unknown, year?: number, month?: number): string {
  const text = norm(value);
  let date: Date;
  if (/^\d+(?:\.\d+)?$/.test(text) && Number(text) > 10000 && Number(text) < 200000)
    date = new Date(Date.UTC(1899, 11, 30) + Math.floor(Number(text)) * 86400000);
  else {
    let match: string[] | null = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?$/.exec(text);
    if (!match && year && month && /^\d{1,2}$/.test(text)) match = [text, String(year), String(month), text];
    if (!match && year && /^\d{1,2}\/\d{1,2}$/.test(text)) match = [text, String(year), ...text.split("/")];
    if (!match) throw new Error("날짜는 연도를 포함해 입력하세요.");
    date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
    if (
      date.getUTCFullYear() !== Number(match[1]) ||
      date.getUTCMonth() + 1 !== Number(match[2]) ||
      date.getUTCDate() !== Number(match[3])
    )
      throw new Error("달력에 없는 날짜입니다.");
  }
  return date.toISOString().slice(0, 10);
}
export function time(value: string): string {
  if (/^(?:0(?:\.\d+)?|\.\d+)$/.test(value)) {
    const minutes = Math.round(Number(value) * 1440);
    if (minutes < 1440)
      return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
  }
  const m = /^(\d{1,2}):([0-5]\d)(?::00)?$/.exec(value.trim());
  if (!m || Number(m[1]) > 23) throw new Error("시간은 HH:MM으로 입력하세요.");
  return `${m[1]?.padStart(2, "0")}:${m[2]}`;
}
export function kstNow(now = new Date()): string {
  return new Date(now.getTime() + 9 * 3600000).toISOString().slice(0, 16);
}
export function addMonths(now: Date, count: number): Date {
  const date = new Date(now);
  const n = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCMonth(date.getUTCMonth() + count);
  const last = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  date.setUTCDate(Math.min(n, last));
  return date;
}
export function validateDraft(draft: Draft, now = new Date(), checkTime = true): string[] {
  const issues: string[] = [];
  try {
    const at = `${day(draft.scheduledAt.slice(0, 10))}T${time(draft.scheduledAt.slice(11))}`;
    if (at !== draft.scheduledAt) throw new Error();
    const parsed = new Date(`${at}:00+09:00`);
    if (checkTime && parsed.getTime() < now.getTime() + 600000)
      issues.push("예약시간은 지금부터 10분 이상 뒤로 지정하세요.");
    if (checkTime && parsed > addMonths(now, 3)) issues.push("예약시간은 현재부터 3개월 이내로 지정하세요.");
  } catch {
    issues.push("예약 날짜와 시간을 확인하세요.");
  }
  if (
    !draft.recipientNames.length ||
    draft.recipientNames.length > 100 ||
    new Set(draft.recipientNames).size !== draft.recipientNames.length ||
    draft.recipientNames.some((name) => !norm(name) || name.includes("\t") || name.includes("\0"))
  )
    issues.push("받는 사람은 중복 없이 1~100명 입력하세요.");
  if (!draft.body.trim()) issues.push("내용을 입력하세요.");
  if (Array.from(draft.title).length > 200 || Array.from(draft.body).length > 30000)
    issues.push("제목은 200자, 내용은 30,000자 이내로 입력하세요.");
  if (
    [draft.title, draft.body].some(
      (s) =>
        s.includes("\0") ||
        s.includes("\t") ||
        /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(s),
    )
  )
    issues.push("탭·널 문자 또는 잘못된 문자를 바꿔 주세요.");
  if (
    draft.attachments.length > 10 ||
    new Set(draft.attachments.map((f) => f.name.toLowerCase())).size !== draft.attachments.length
  )
    issues.push("첨부파일은 서로 다른 이름으로 10개까지 선택하세요.");
  if (draft.missingAttachments.length) issues.push("저장한 첨부파일을 다시 선택하거나 첨부 목록에서 제거하세요.");
  return issues;
}
export function createDraft(input: Partial<Draft> = {}): Draft {
  return {
    id: crypto.randomUUID(),
    recipientNames: [],
    bindings: {},
    title: "",
    body: "",
    scheduledAt: kstNow(new Date(Date.now() + 20 * 60000)),
    attachments: [],
    missingAttachments: [],
    revision: 1,
    state: "draft",
    retryLocked: false,
    archived: false,
    ...input,
  };
}
export function content(draft: Draft): string {
  return JSON.stringify([
    draft.scheduledAt,
    draft.title,
    draft.body,
    Object.entries(draft.bindings)
      .map(([name, r]) => [name, evidenceKey(r)])
      .sort(),
    draft.attachments.map((f) => [f.name, f.size]).sort(),
    draft.missingAttachments,
  ]);
}
export function scheduledMessage(
  draft: Draft,
  recipients: MessagingRecipient[],
): { message: ScheduledMessage; issues: string[] } {
  const resolved = resolveNames(draft.recipientNames, draft.bindings, recipients, true);
  return {
    message: {
      id: draft.id,
      recipients: resolved.handles,
      title: draft.title || null,
      body: draft.body,
      scheduledAt: draft.scheduledAt,
      attachmentHandles: draft.attachments.map((f) => f.handle),
      contentRevision: draft.revision,
    },
    issues: [
      ...validateDraft(draft),
      ...resolved.issues,
      ...(draft.retryLocked ? ["등록 시도 기록이 있어 다시 실행할 수 없습니다."] : []),
    ],
  };
}
export function repeat(base: Draft, end: string, weekdays: number[], exclusions: string[], now = new Date()): Draft[] {
  const start = day(base.scheduledAt.slice(0, 10)),
    until = day(end),
    cutoff = kstNow(addMonths(now, 3)).slice(0, 10);
  if (
    until < start ||
    until > cutoff ||
    !weekdays.length ||
    weekdays.some((n) => !Number.isInteger(n) || n < 0 || n > 6)
  )
    throw new Error("반복 요일과 시작일부터 3개월 이내 종료일을 확인하세요.");
  const skipped = new Set(exclusions.map((x) => day(x))),
    result: Draft[] = [];
  for (
    let at = new Date(`${start}T00:00:00Z`);
    at.toISOString().slice(0, 10) <= until;
    at = new Date(at.getTime() + 86400000)
  ) {
    const date = at.toISOString().slice(0, 10);
    if (weekdays.includes((at.getUTCDay() + 6) % 7) && !skipped.has(date)) {
      const next = createDraft({
        ...base,
        id: crypto.randomUUID(),
        scheduledAt: `${date}T${base.scheduledAt.slice(11)}`,
      });
      delete next.workflow;
      result.push(next);
    }
  }
  if (!result.length || result.length > 500) throw new Error("반복 조건에 맞는 날짜가 없거나 500건을 넘습니다.");
  return result;
}
export function parseTable(text: string, delimiter: string = text.includes("\t") ? "\t" : ","): string[][] {
  if (new TextEncoder().encode(text).length > 10 * 1024 * 1024) throw new Error("표는 10MiB 이하로 입력하세요.");
  const rows: string[][] = [];
  let row: string[] = [],
    cell = "",
    quoted = false,
    closed = false;
  const pushCell = () => {
    row.push(cell);
    cell = "";
    closed = false;
    if (row.length > 128) throw new Error("표는 128열 이내여야 합니다.");
  };
  const pushRow = () => {
    pushCell();
    rows.push(row);
    row = [];
    if (rows.length > 510) throw new Error("표는 제목행을 포함해 510행 이내여야 합니다.");
  };
  const input = text.replace(/^\ufeff/, "");
  for (let i = 0; i < input.length; i++) {
    const c = input[i] ?? "";
    if (quoted) {
      if (c === '"') {
        if (input[i + 1] === '"') {
          cell += '"';
          i++;
        } else {
          quoted = false;
          closed = true;
        }
      } else cell += c;
    } else if (c === '"') {
      if (cell || closed) throw new Error("표의 따옴표를 확인하세요.");
      quoted = true;
    } else if (c === delimiter) pushCell();
    else if (c === "\r" || c === "\n") {
      if (c === "\r" && input[i + 1] === "\n") i++;
      pushRow();
    } else {
      if (closed) throw new Error("표의 따옴표 뒤에는 열 또는 행 구분만 올 수 있습니다.");
      cell += c;
    }
  }
  if (quoted) throw new Error("표의 따옴표를 닫아 주세요.");
  if (cell || row.length || closed) pushRow();
  return rows;
}
export function batchDrafts(values: string[][]): Draft[] {
  const rows = values.filter((row) => row.some((x) => norm(x)));
  const headers = rows[0]?.map((x) => norm(x).replaceAll(" ", "")) ?? [];
  if (
    !headers.includes("받는사람") ||
    !headers.includes("내용") ||
    (!headers.includes("예약일시") && !(headers.includes("예약날짜") && headers.includes("예약시간")))
  )
    throw new Error("받는사람·예약날짜·예약시간·내용 열이 필요합니다. 제목은 선택이며 예약일시 열도 읽습니다.");
  if (new Set(headers).size !== headers.length || headers.some((x) => !x) || rows.length < 2 || rows.length > 501)
    throw new Error("중복·빈 열 이름 없이 1~500건 입력하세요.");
  return rows.slice(1).map((row) => {
    if (row.slice(headers.length).some((x) => norm(x)))
      throw new Error("열 이름보다 값이 많습니다. 본문의 쉼표는 따옴표로 묶으세요.");
    const get = (name: string) => row[headers.indexOf(name)] ?? "";
    const combined = norm(get("예약일시")).replace(" ", "T");
    let combinedAt = "";
    if (combined) {
      if (/^\d+(?:\.\d+)?$/.test(combined) && Number(combined) > 10000)
        combinedAt = `${day(combined)}T${time(String(Number(combined) % 1))}`;
      else {
        const pieces = combined.split("T");
        if (pieces.length !== 2) throw new Error("예약일시를 확인하세요.");
        combinedAt = `${day(pieces[0])}T${time(pieces[1] ?? "")}`;
      }
    }
    const attachments = get("첨부파일")
      .split(/[;\r\n]/)
      .map(norm)
      .filter(Boolean);
    return createDraft({
      recipientNames: names(get("받는사람")),
      title: norm(get("제목")),
      body: norm(get("내용")),
      scheduledAt: combined ? combinedAt : `${day(get("예약날짜"))}T${time(get("예약시간"))}`,
      missingAttachments: attachments.map(() => "예약표의 첨부파일"),
      attachments: [],
    });
  });
}
export const stateLabel: Record<MessagingItemState, string> = {
  draft: "초안",
  preparing: "입력 중",
  prepared: "자동 입력 확인 완료",
  submitting: "등록 중",
  "registration-requested": "등록 요청 완료",
  confirmed: "확인 완료",
  uncertain: "등록 여부 불명",
  failed: "중단",
};
