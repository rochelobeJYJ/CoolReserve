// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import { Unzip, UnzipInflate } from "fflate";
import { parseTable } from "./logic.ts";
const worker = self as unknown as {
  onmessage: (event: MessageEvent<{ bytes: Uint8Array; format: "zip" | "csv" | "paste" }>) => void;
  postMessage: (message: unknown, transfer?: Transferable[]) => void;
};
export function unpack(bytes: Uint8Array): Record<string, Uint8Array> {
  if (bytes.length > 10 * 1024 * 1024) throw new Error("INPUT_LIMIT");
  const parts: Record<string, Uint8Array> = Object.create(null) as Record<string, Uint8Array>;
  let total = 0;
  let files = 0;
  let ended = 0;
  const seen = new Set<string>();
  const zip = new Unzip((file) => {
    if (
      ++files > 4096 ||
      seen.has(file.name) ||
      file.name.startsWith("/") ||
      /[\\:%?#]/.test(file.name) ||
      file.name.split("/").some((p) => p === ".." || p === ".")
    )
      throw new Error("ARCHIVE_LIMIT");
    seen.add(file.name);
    if (file.compression !== 0 && file.compression !== 8) throw new Error("ARCHIVE_LIMIT");
    const wanted = /^(?:xl\/.*\.(?:xml|rels)|Contents\/section\d+\.xml)$/.test(file.name);
    const chunks: Uint8Array[] = [];
    let length = 0;
    file.ondata = (error, chunk, final) => {
      if (error) throw new Error("INVALID_ARCHIVE");
      total += chunk.length;
      length += chunk.length;
      if (total > 64 * 1024 * 1024 || (wanted && length > 16 * 1024 * 1024)) {
        file.terminate();
        throw new Error("ARCHIVE_LIMIT");
      }
      if (wanted) chunks.push(chunk);
      if (final) {
        ended++;
        if (wanted) {
          const data = new Uint8Array(length);
          let at = 0;
          for (const part of chunks) {
            data.set(part, at);
            at += part.length;
          }
          parts[file.name] = data;
        }
      }
    };
    file.start();
  });
  zip.register(UnzipInflate);
  for (let at = 0; at < bytes.length; at += 4096) zip.push(bytes.subarray(at, at + 4096), at + 4096 >= bytes.length);
  if (!files || files !== ended) throw new Error("INVALID_ARCHIVE");
  return parts;
}
worker.onmessage = (event) => {
  try {
    if (event.data.format === "zip") worker.postMessage({ parts: unpack(event.data.bytes) });
    else {
      let text: string;
      try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(event.data.bytes);
      } catch {
        if (event.data.format !== "csv") throw new Error("INVALID_TEXT");
        text = new TextDecoder("euc-kr", { fatal: true }).decode(event.data.bytes);
      }
      worker.postMessage({
        values: parseTable(text, event.data.format === "csv" ? "," : text.includes("\t") ? "\t" : ","),
      });
    }
  } catch {
    worker.postMessage({ error: "IMPORT_FAILED" });
  }
};
