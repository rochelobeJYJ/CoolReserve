// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
// @vitest-environment jsdom
import { unzipSync } from "fflate";
import { describe, expect, it } from "vitest";
import { workbook, workbookTemplate } from "./workbook.ts";
import { batchDrafts } from "./logic.ts";
describe("editable source workbook compatibility", () => {
  it("reopens actual XLSX with multiline body, blank title and all editable source columns", async () => {
    const rows = [
      ["받는사람", "예약날짜", "예약시간", "제목", "내용"],
      ["가상교사가", "2026-10-12", "12:00", "", "줄1 & <본문>\n줄2"],
    ];
    const parsed = await workbook(unzipSync(workbookTemplate(rows)), new AbortController().signal);
    const cells = parsed.sheets[0]?.cells ?? [];
    const values = rows.map((row, index) =>
      row.map((_, col) => cells.find((c) => c.row === index + 1 && c.col === col + 1)?.value ?? ""),
    );
    const result = batchDrafts(values);
    expect(result[0]?.title).toBe("");
    expect(result[0]?.body).toBe("줄1 & <본문>\n줄2");
    expect(result[0]?.scheduledAt).toBe("2026-10-12T12:00");
  });
  it("supports OPC package-root relations and rejects escaping or external references", async () => {
    const parts = unzipSync(workbookTemplate([["합성열"], ["합성값"]]));
    const rel = parts["xl/_rels/workbook.xml.rels"];
    if (!rel) throw new Error("TEST_SETUP");
    const original = new TextDecoder().decode(rel);
    parts["xl/_rels/workbook.xml.rels"] = new TextEncoder().encode(
      original.replace("worksheets/sheet1.xml", "/xl/worksheets/sheet1.xml"),
    );
    expect((await workbook(parts, new AbortController().signal)).sheets).toHaveLength(1);
    parts["xl/_rels/workbook.xml.rels"] = new TextEncoder().encode(
      original.replace("worksheets/sheet1.xml", "/../sheet1.xml"),
    );
    await expect(workbook(parts, new AbortController().signal)).rejects.toThrow();
  });
  it("corrects explicit 1904 date cells without altering account strings", async () => {
    const parts = unzipSync(workbookTemplate([["예약일시"], ["합성값"]]));
    parts["xl/workbook.xml"] = new TextEncoder().encode(
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><workbookPr date1904="1"/><sheets><sheet name="합성" r:id="rId1"/></sheets></workbook>',
    );
    parts["xl/styles.xml"] = new TextEncoder().encode(
      '<styleSheet><cellXfs><xf numFmtId="22"/></cellXfs></styleSheet>',
    );
    parts["xl/worksheets/sheet1.xml"] = new TextEncoder().encode(
      '<worksheet><sheetData><row><c r="A1" s="0"><v>44000.5</v></c><c r="B1" t="inlineStr"><is><t>00101</t></is></c></row></sheetData></worksheet>',
    );
    const cells = (await workbook(parts, new AbortController().signal)).sheets[0]?.cells;
    expect(cells?.[0]?.value).toBe("45462.5");
    expect(cells?.[1]?.value).toBe("00101");
  });
});
