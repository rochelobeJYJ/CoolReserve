// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import { zipSync } from "fflate";
/** Original-style editable reservation/workflow templates with exact text cells. */
export function workbookTemplate(data: string[][]): Uint8Array {
  const parts: Record<string, Uint8Array> = {};
  const encode = (name: string, text: string) => {
    parts[name] = new TextEncoder().encode(text);
  };
  encode(
    "[Content_Types].xml",
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
  );
  encode(
    "_rels/.rels",
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
  );
  encode(
    "xl/workbook.xml",
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="연결 자료" sheetId="1" r:id="rId1"/></sheets></workbook>',
  );
  encode(
    "xl/_rels/workbook.xml.rels",
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
  );
  encode(
    "xl/worksheets/sheet1.xml",
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' +
      data
        .map(
          (row, index) =>
            `<row r="${index + 1}">${row.map((value, col) => `<c r="${String.fromCharCode(65 + col)}${index + 1}" t="inlineStr"><is><t>${value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")}</t></is></c>`).join("")}</row>`,
        )
        .join("") +
      "</sheetData></worksheet>",
  );
  return zipSync(parts, { level: 6 });
}
export interface Worksheet {
  name: string;
  cells: { row: number; col: number; value: string }[];
}
export interface Workbook {
  sheets: Worksheet[];
}
export type Parts = Record<string, Uint8Array>;
const elements = (node: Document | Element, name: string) => Array.from(node.getElementsByTagNameNS("*", name));
function xml(bytes: Uint8Array): Document {
  if (bytes.length > 16 * 1024 * 1024) throw new Error("XML_LIMIT");
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  if (/<!\s*(DOCTYPE|ENTITY)/i.test(text)) throw new Error("INVALID_XML");
  const doc = new DOMParser().parseFromString(text, "application/xml");
  if (elements(doc, "parsererror").length) throw new Error("INVALID_XML");
  return doc;
}
function partPath(source: string, target: string): string {
  if (
    !target ||
    target.startsWith("//") ||
    /[\\:%?#]/.test(target) ||
    Array.from(target).some((char) => char.charCodeAt(0) < 32)
  )
    throw new Error("INVALID_WORKBOOK");
  const rooted = target.startsWith("/");
  const path = rooted ? [] : source.split("/");
  if (!rooted) path.pop();
  for (const item of (rooted ? target.slice(1) : target).split("/")) {
    if (item === "..") {
      if (!path.length) throw new Error("INVALID_WORKBOOK");
      path.pop();
    } else if (item !== "." && item) path.push(item);
    else if (!item) throw new Error("INVALID_WORKBOOK");
  }
  return path.join("/");
}
export async function workbook(parts: Parts, signal: AbortSignal): Promise<Workbook> {
  const shared = parts["xl/sharedStrings.xml"]
    ? elements(xml(parts["xl/sharedStrings.xml"]), "si").map((si) =>
        elements(si, "t")
          .map((t) => t.textContent ?? "")
          .join(""),
      )
    : [];
  const formats = new Map<string, string>();
  let styles: string[] = [];
  let dateStyles: boolean[] = [];
  if (parts["xl/styles.xml"]) {
    const doc = xml(parts["xl/styles.xml"]);
    for (const format of elements(doc, "numFmt"))
      formats.set(format.getAttribute("numFmtId") ?? "", format.getAttribute("formatCode") ?? "");
    const xfs = elements(doc, "cellXfs")[0];
    if (xfs) {
      const list = elements(xfs, "xf");
      styles = list.map((xf) => formats.get(xf.getAttribute("numFmtId") ?? "") ?? "");
      dateStyles = list.map((xf) => {
        const id = Number(xf.getAttribute("numFmtId"));
        return (id >= 14 && id <= 22) || /[yd]/i.test(formats.get(String(id)) ?? "");
      });
    }
  }
  const main = parts["xl/workbook.xml"];
  const relBytes = parts["xl/_rels/workbook.xml.rels"];
  if (!main || !relBytes) throw new Error("INVALID_WORKBOOK");
  const mainDoc = xml(main),
    date1904 = ["1", "true"].includes(elements(mainDoc, "workbookPr")[0]?.getAttribute("date1904") ?? "");
  const rels = new Map<string, string>();
  for (const rel of elements(xml(relBytes), "Relationship")) {
    if ((rel.getAttribute("TargetMode") ?? "").toLowerCase() === "external") throw new Error("INVALID_WORKBOOK");
    const id = rel.getAttribute("Id") ?? "";
    if (!id || rels.has(id)) throw new Error("INVALID_WORKBOOK");
    rels.set(id, partPath("xl/workbook.xml", rel.getAttribute("Target") ?? ""));
  }
  const sheets: Worksheet[] = [];
  let total = 0;
  for (const sheet of elements(mainDoc, "sheet")) {
    if (sheets.length >= 100) throw new Error("WORKBOOK_LIMIT");
    const id = Array.from(sheet.attributes).find((a) => a.localName === "id")?.value ?? "";
    const path = rels.get(id);
    const bytes = path ? parts[path] : undefined;
    if (!bytes) throw new Error("INVALID_WORKBOOK");
    const cells: Worksheet["cells"] = [];
    const seen = new Set<string>();
    for (const cell of elements(xml(bytes), "c")) {
      if (++total > 100000) throw new Error("WORKBOOK_LIMIT");
      if (total % 500 === 0) {
        await new Promise<void>((resolve) => setTimeout(resolve, 0));
        if (signal.aborted) throw new Error("CANCELLED");
      }
      const ref = cell.getAttribute("r") ?? "";
      const match = /^([A-Z]{1,3})(\d+)$/.exec(ref);
      if (!match || seen.has(ref)) throw new Error("INVALID_WORKBOOK");
      seen.add(ref);
      const row = Number(match[2]);
      let col = 0;
      for (const c of match[1] ?? "") col = col * 26 + c.charCodeAt(0) - 64;
      if (row > 1048576 || col > 16384) throw new Error("WORKBOOK_LIMIT");
      const raw = elements(cell, "v")[0]?.textContent ?? "";
      let value =
        cell.getAttribute("t") === "s"
          ? (shared[Number(raw)] ?? "")
          : cell.getAttribute("t") === "inlineStr"
            ? elements(cell, "t")
                .map((t) => t.textContent ?? "")
                .join("")
            : raw;
      const numeric = !cell.hasAttribute("t") || cell.getAttribute("t") === "n";
      const format = styles[Number(cell.getAttribute("s") ?? 0)];
      if (
        numeric &&
        date1904 &&
        dateStyles[Number(cell.getAttribute("s") ?? 0)] &&
        Number(value) >= 1 &&
        /^\d+(?:\.\d+)?$/.test(value)
      )
        value = String(Number(value) + 1462);
      if (numeric && format && /^0{1,32}$/.test(format) && /^\d+$/.test(value))
        value = value.padStart(format.length, "0");
      if (value.length > 30000) throw new Error("WORKBOOK_LIMIT");
      cells.push({ row, col, value });
    }
    sheets.push({ name: sheet.getAttribute("name") ?? `시트${sheets.length + 1}`, cells });
  }
  if (!sheets.length) throw new Error("INVALID_WORKBOOK");
  return { sheets };
}
