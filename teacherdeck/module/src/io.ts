// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import type { Deck, FileHandleInfo } from "@deck/sdk";
import { stateLabel, type Draft } from "./logic.ts";
import { workbook, workbookTemplate, type Workbook, type Parts } from "./workbook.ts";
export async function readBytes(deck: Deck, file: FileHandleInfo, signal: AbortSignal): Promise<Uint8Array> {
  if (file.size > 10 * 1024 * 1024) throw new Error("엑셀·CSV 파일은 10MiB 이하로 선택하세요.");
  const chunks: Uint8Array[] = [];
  let size = 0;
  for await (const chunk of deck.fs.readChunks({ handle: file.handle }, { signal })) {
    size += chunk.length;
    if (size > 10 * 1024 * 1024) throw new Error("입력 파일 크기 제한을 넘었습니다.");
    chunks.push(chunk);
  }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, at);
    at += chunk.length;
  }
  return bytes;
}
function importRequest(
  input: Uint8Array,
  signal: AbortSignal,
  format: "zip" | "csv" | "paste",
): Promise<{ parts?: Parts; values?: string[][] }> {
  if (input.length > 10 * 1024 * 1024) return Promise.reject(new Error("자료는 10MiB 이하로 입력하세요."));
  if (signal.aborted) return Promise.reject(new Error("자료 읽기를 중지했습니다."));
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL("./archive.worker.ts", import.meta.url), { type: "module" });
    const cleanup = () => {
      clearTimeout(timeout);
      worker.terminate();
      signal.removeEventListener("abort", abort);
    };
    const abort = () => {
      cleanup();
      reject(new Error("자료 읽기를 중지했습니다."));
    };
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error("자료 읽기 시간이 초과했습니다. 더 작은 표로 다시 시도하세요."));
    }, 90000);
    signal.addEventListener("abort", abort, { once: true });
    worker.onerror = () => {
      cleanup();
      reject(new Error("엑셀 파일을 읽지 못했습니다. CSV 또는 표 붙여넣기로 다시 시도하세요."));
    };
    worker.onmessage = (event: MessageEvent<{ parts?: Parts; values?: string[][]; error?: string }>) => {
      cleanup();
      if (!event.data.parts && !event.data.values)
        reject(new Error("자료를 읽지 못했습니다. 형식과 표의 따옴표·행·열을 확인하세요."));
      else resolve(event.data);
    };
    const bytes = input.slice();
    worker.postMessage({ bytes, format }, [bytes.buffer]);
  });
}
export async function archiveRequest(input: Uint8Array, signal: AbortSignal): Promise<Parts> {
  const result = await importRequest(input, signal, "zip");
  if (!result.parts) throw new Error("엑셀 형식을 확인하세요.");
  return result.parts;
}
export async function parseText(text: string, signal: AbortSignal): Promise<string[][]> {
  const result = await importRequest(new TextEncoder().encode(text), signal, "paste");
  if (!result.values) throw new Error("표를 확인하세요.");
  return result.values;
}
export async function readTable(
  deck: Deck,
  file: FileHandleInfo,
  signal: AbortSignal,
): Promise<{ name: string; values: string[][] }[]> {
  const bytes = await readBytes(deck, file, signal);
  if (file.ext.toLowerCase() === "csv") {
    const result = await importRequest(bytes, signal, "csv");
    if (!result.values) throw new Error("CSV를 확인하세요.");
    return [{ name: "CSV", values: result.values }];
  }
  if (file.ext.toLowerCase() !== "xlsx") throw new Error("엑셀 XLSX 또는 CSV를 선택하세요.");
  const book: Workbook = await workbook(await archiveRequest(bytes, signal), signal);
  return book.sheets.map((sheet) => {
    const maxRow = sheet.cells.reduce((max, cell) => Math.max(max, cell.row), 0),
      maxCol = sheet.cells.reduce((max, cell) => Math.max(max, cell.col), 0);
    if (maxRow > 510 || maxCol > 128) throw new Error("자료는 제목행 포함 510행·128열 이내로 나눠 주세요.");
    const values = Array.from({ length: maxRow }, () => Array<string>(maxCol).fill(""));
    for (const cell of sheet.cells) {
      const row = values[cell.row - 1];
      if (row) row[cell.col - 1] = cell.value;
    }
    return { name: sheet.name, values };
  });
}
export function historyCsv(drafts: Draft[]): Uint8Array {
  const safe = (value: string) => '"' + (/^[=+@-]/.test(value) ? "'" + value : value).replaceAll('"', '""') + '"';
  const rows = [
    ["예약일시", "받는사람", "제목", "내용", "상태", "보관"],
    ...drafts.map((d) => [
      d.scheduledAt.replace("T", " "),
      d.recipientNames.join(" · "),
      d.title,
      d.body,
      stateLabel[d.state],
      d.archived ? "보관" : "목록",
    ]),
  ];
  return new TextEncoder().encode("\ufeff" + rows.map((row) => row.map(safe).join(",")).join("\r\n"));
}
export async function saveFile(
  deck: Deck,
  name: string,
  bytes: Uint8Array,
  source?: FileHandleInfo,
  chooseFolder = false,
): Promise<boolean> {
  const location =
    source && !chooseFolder
      ? { sourceHandle: source.handle }
      : await deck.fs.pickFolder().then((folder) => (folder ? { parentHandle: folder.handle } : null));
  if (!location) return false;
  const batch = await deck.fs.openOutputFolder(location);
  try {
    await deck.fs.writeBlob({ batchId: batch.batchId, suggestedName: name, blob: new Blob([bytes.slice().buffer]) });
  } finally {
    await deck.fs.closeOutputFolder({ batchId: batch.batchId });
  }
  return true;
}
export async function saveTemplate(deck: Deck, preset: "batch" | "general" | "lunch"): Promise<boolean> {
  const data =
    preset === "batch"
      ? [["받는사람", "예약날짜", "예약시간", "제목", "내용", "첨부파일"]]
      : preset === "general"
        ? [["업무일", "담당자", "제목", "내용"]]
        : [["날짜", "1차 담당자", "2차 담당자", "제외일", "고사 1차만"]];
  return saveFile(
    deck,
    preset === "lunch" ? "중식지도_프리셋.xlsx" : preset === "general" ? "업무자료_양식.xlsx" : "예약메시지_양식.xlsx",
    workbookTemplate(data),
  );
}
