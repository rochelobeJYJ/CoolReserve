// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
// @vitest-environment jsdom
import { connect, type Deck, type FileHandleInfo } from "@deck/sdk";
import { createMockHost } from "@deck/sdk/testing";
import { DeckProvider } from "@deck/ui";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App.tsx";
import { initialState, backup } from "./state.ts";
import { saveFile } from "./io.ts";
vi.mock("./io.ts", async (importActual) => {
  const actual = await importActual<typeof import("./io.ts")>();
  return {
    ...actual,
    readTable: async () =>
      ["합성시트", "합성다음시트"].map((name) => ({
        name,
        values: [
          ["받는사람", "예약날짜", "예약시간", "내용"],
          ["합성교사가", "2027-01-01", "09:00", "합성 안내"],
        ],
      })),
    parseText: async (text: string) => {
      if (text === "INVALID") throw new Error("SYNTHETIC_PASTE_ERROR");
      return text.split("\n").map((line) => line.split("\t"));
    },
  };
});
const seed = () => initialState();
const save = (deck: Deck, source?: FileHandleInfo, chooseFolder = false) =>
  saveFile(deck, "합성백업.json", backup(seed()), source, chooseFolder);
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
async function setup() {
  const bytes = backup(seed());
  const file: FileHandleInfo = {
    handle: "source",
    name: "합성백업.json",
    ext: "json",
    size: bytes.length,
    modifiedAt: 0,
  };
  const origin = "http://deckmod.coolreserve.modules.localhost";
  const values = new Map<string, unknown>();
  let written = 0;
  const host = createMockHost({
    module: { id: "coolreserve", version: "0.2.0" },
    granted: ["fs", "storage", "window", "messaging"],
    handlers: {
      "storage.get": (args) => values.get((args as { key: string }).key) ?? null,
      "storage.set": (args) => {
        const a = args as { key: string; value: unknown };
        values.set(a.key, a.value);
        return null;
      },
      "storage.keys": () => [...values.keys()],
      "storage.delete": (args) => {
        values.delete((args as { key: string }).key);
        return null;
      },
      "fs.pickFiles": () => {
        throw new Error("UNEXPECTED_FILE_PICKER");
      },
      "fs.pickFolder": () => ({ handle: "chosen", name: "합성폴더" }),
      "fs.openRead": () => ({
        readId: "read",
        size: bytes.length,
        chunkBytes: 64,
        url: origin + "/_resources/" + "a".repeat(32),
      }),
      "fs.closeRead": () => null,
      "fs.openOutputFolder": () => ({ batchId: "batch", folder: { handle: "output", name: "합성폴더" } }),
      "fs.beginWrite": () => {
        written = 0;
        return { writeId: "write", chunkBytes: 65536 };
      },
      "fs.writeChunk": (args) => {
        const a = args as { offset: number; data: number[] };
        written += a.data.length;
        return { nextOffset: a.offset + a.data.length };
      },
      "fs.commitWrite": () => ({ ...file, handle: "saved", size: written }),
      "fs.abortWrite": () => null,
      "fs.closeOutputFolder": () => null,
    },
  });
  host.window.location = { origin };
  const deck = await connect({ window: host.window });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const q = new URL(url).searchParams;
      const offset = Number(q.get("offset"));
      return new Response(bytes.slice(offset, offset + Number(q.get("length"))));
    }),
  );
  return { host, deck, file };
}
async function open() {
  const context = await setup();
  const view = render(
    <DeckProvider>
      <App deck={context.deck} />
    </DeckProvider>,
  );
  await waitFor(() =>
    expect(
      [...view.container.querySelectorAll("button")].some(
        (button) => button.textContent?.trim() === "백업 복구" && !button.disabled,
      ),
    ).toBe(true),
  );
  return { ...context, view };
}
describe("native file input and direct output", () => {
  it("accepts a dropped backup and visibly identifies it without opening either picker", async () => {
    const { host, deck, file } = await open();
    await act(async () => host.emit("fs.dropped", { files: [file] }));
    await waitFor(() => expect(screen.getAllByText(/합성백업.json/).length).toBeGreaterThan(0));
    await waitFor(() => expect(host.requests.some((r) => r.method === "closeRead")).toBe(true));
    expect(host.requests.some((r) => r.method === "pickFiles" || r.method === "pickFolder")).toBe(false);
    deck.dispose();
  });
  it("keeps the received filename visible while the native read is still pending", async () => {
    const { host, deck, file } = await open();
    let resume!: () => void;
    const gate = new Promise<void>((resolve) => {
      resume = resolve;
    });
    const fetchChunk = globalThis.fetch;
    vi.stubGlobal("fetch", async (url: string) => {
      await gate;
      return fetchChunk(url);
    });
    await act(async () => host.emit("fs.dropped", { files: [file] }));
    try {
      await screen.findByText(/합성백업.json.*읽고 있어요/);
      await waitFor(() => expect(host.requests.some((r) => r.method === "openRead")).toBe(true));
      expect(host.requests.some((r) => r.method === "closeRead" || r.method === "pickFiles")).toBe(false);
    } finally {
      resume();
    }
    await waitFor(() => expect(host.requests.some((r) => r.method === "closeRead")).toBe(true));
    deck.dispose();
  });
  it("rejects multiple dropped imports visibly instead of silently choosing one", async () => {
    const { host, deck, file } = await open();
    await act(async () => host.emit("fs.dropped", { files: [file, { ...file, handle: "second" }] }));
    await screen.findByText(/1개씩/);
    expect(host.requests.some((r) => r.method === "openRead" || r.method === "pickFiles")).toBe(false);
    deck.dispose();
  });
  it("attaches dropped files in the message editor and preserves the attachment limit", async () => {
    const { host, deck, file } = await open();
    fireEvent.click(screen.getByRole("button", { name: "새 메시지" }));
    fireEvent.click(await screen.findByRole("button", { name: "첨부 · 반복 예약" }));
    await act(async () => host.emit("fs.dropped", { files: [{ ...file, name: "합성첨부.png", ext: "png" }] }));
    await screen.findByText("합성첨부.png");
    expect(host.requests.some((r) => r.method === "pickFiles" || r.method === "openRead")).toBe(false);
    await act(async () =>
      host.emit("fs.dropped", {
        files: Array.from({ length: 11 }, (_, index) => ({
          ...file,
          handle: `attachment-${index}`,
          name: `합성첨부${index}.txt`,
          ext: "txt",
        })),
      }),
    );
    await screen.findByText(/첨부파일은 10개까지/);
    expect(screen.getByText("합성첨부.png")).toBeTruthy();
    expect(screen.queryByText("합성첨부0.txt")).toBeNull();
    deck.dispose();
  });
  it("clears the previous table source after restoring a different full backup", async () => {
    const { host, deck, file } = await open();
    await act(async () =>
      host.emit("fs.dropped", { files: [{ ...file, handle: "table-a", name: "합성자료A.csv", ext: "csv" }] }),
    );
    await waitFor(() => expect(screen.getAllByText(/합성자료A.csv.*읽었어요/).length).toBeGreaterThan(0));
    const backupFile = { ...file, handle: "backup-b", name: "합성백업B.json" };
    await act(async () => host.emit("fs.dropped", { files: [backupFile] }));
    await screen.findByText(/합성백업B.json에서 백업을 복구했어요/);
    fireEvent.click(screen.getByRole("button", { name: "실행 기록" }));
    fireEvent.click(await screen.findByRole("button", { name: "업무 기록 CSV 저장 · 수신자와 내용 포함" }));
    await waitFor(() => expect(host.requests.some((r) => r.method === "openOutputFolder")).toBe(true));
    expect(host.requests.find((r) => r.method === "openOutputFolder")?.args).toEqual({ sourceHandle: "backup-b" });
    expect(host.requests.some((r) => r.method === "pickFolder")).toBe(false);
    deck.dispose();
  });
  it.each(["batch", "workflow"])("clears the prior file source after a successful %s paste", async (kind) => {
    const { host, deck, file } = await open();
    await act(async () => host.emit("fs.dropped", { files: [{ ...file, ext: "xlsx", name: "합성자료A.xlsx" }] }));
    await screen.findByText(/합성자료A.xlsx.*읽었어요/);
    if (kind === "workflow") {
      fireEvent.click(screen.getByRole("button", { name: "예약 메시지" }));
      fireEvent.click(screen.getByRole("button", { name: "자료로 메시지 만들기" }));
    }
    const expander = screen.getByRole("button", { name: kind === "batch" ? "표 붙여넣기" : "자료 표 붙여넣기" });
    if (expander.getAttribute("aria-expanded") === "false") fireEvent.click(expander);
    fireEvent.change(
      screen.getByRole("textbox", { name: kind === "batch" ? "열 이름을 포함한 표" : "열 이름을 포함한 자료" }),
      { target: { value: "받는사람\t예약날짜\t예약시간\t내용\n합성교사나\t2027-01-02\t09:00\t합성 추가" } },
    );
    const apply = screen.getByRole("button", { name: kind === "batch" ? "입력표로 옮기기" : "자료 읽기" });
    await act(async () => {
      fireEvent.click(apply);
    });
    await waitFor(() => expect((apply as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "실행 기록" }));
    fireEvent.click(await screen.findByRole("button", { name: "업무 기록 CSV 저장 · 수신자와 내용 포함" }));
    await waitFor(() => expect(host.requests.some((r) => r.method === "openOutputFolder")).toBe(true));
    expect(host.requests.find((r) => r.method === "openOutputFolder")?.args).toEqual({ parentHandle: "chosen" });
    expect(host.requests.some((r) => r.method === "pickFolder")).toBe(true);
    deck.dispose();
  });
  it.each(["batch", "workflow"])("preserves the existing source after a failed %s paste", async (kind) => {
    const { host, deck, file } = await open();
    await act(async () => host.emit("fs.dropped", { files: [{ ...file, ext: "xlsx", name: "합성자료A.xlsx" }] }));
    await screen.findByText(/합성자료A.xlsx.*읽었어요/);
    if (kind === "workflow") {
      fireEvent.click(screen.getByRole("button", { name: "예약 메시지" }));
      fireEvent.click(screen.getByRole("button", { name: "자료로 메시지 만들기" }));
    }
    const expander = screen.getByRole("button", { name: kind === "batch" ? "표 붙여넣기" : "자료 표 붙여넣기" });
    if (expander.getAttribute("aria-expanded") === "false") fireEvent.click(expander);
    fireEvent.change(
      screen.getByRole("textbox", { name: kind === "batch" ? "열 이름을 포함한 표" : "열 이름을 포함한 자료" }),
      { target: { value: "INVALID" } },
    );
    fireEvent.click(screen.getByRole("button", { name: kind === "batch" ? "입력표로 옮기기" : "자료 읽기" }));
    await screen.findByText("SYNTHETIC_PASTE_ERROR");
    fireEvent.click(screen.getByRole("button", { name: "실행 기록" }));
    fireEvent.click(await screen.findByRole("button", { name: "업무 기록 CSV 저장 · 수신자와 내용 포함" }));
    await waitFor(() => expect(host.requests.some((r) => r.method === "openOutputFolder")).toBe(true));
    expect(host.requests.find((r) => r.method === "openOutputFolder")?.args).toEqual({ sourceHandle: file.handle });
    expect(host.requests.some((r) => r.method === "pickFolder")).toBe(false);
    deck.dispose();
  });
  it("restores the loaded file source when selecting an original sheet after a pasted batch", async () => {
    const { host, deck, file } = await open();
    await act(async () => host.emit("fs.dropped", { files: [{ ...file, ext: "xlsx", name: "합성자료A.xlsx" }] }));
    await screen.findByText(/합성자료A.xlsx.*읽었어요/);
    const expander = screen.getByRole("button", { name: "표 붙여넣기" });
    if (expander.getAttribute("aria-expanded") === "false") fireEvent.click(expander);
    fireEvent.change(screen.getByRole("textbox", { name: "열 이름을 포함한 표" }), {
      target: { value: "받는사람\t예약날짜\t예약시간\t내용\n합성교사나\t2027-01-02\t09:00\t합성 추가" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "입력표로 옮기기" }));
    });
    fireEvent.click(screen.getByRole("combobox", { name: "가져올 시트" }));
    fireEvent.click(await screen.findByRole("option", { name: "합성다음시트" }));
    fireEvent.click(screen.getByRole("button", { name: "실행 기록" }));
    fireEvent.click(await screen.findByRole("button", { name: "업무 기록 CSV 저장 · 수신자와 내용 포함" }));
    await waitFor(() => expect(host.requests.some((r) => r.method === "openOutputFolder")).toBe(true));
    expect(host.requests.find((r) => r.method === "openOutputFolder")?.args).toEqual({ sourceHandle: file.handle });
    expect(host.requests.some((r) => r.method === "pickFolder")).toBe(false);
    deck.dispose();
  });
  it("saves next to the associated source without choosing or creating a folder", async () => {
    const { host, deck, file } = await setup();
    expect(await save(deck, file)).toBe(true);
    expect(host.requests.find((r) => r.method === "openOutputFolder")?.args).toEqual({ sourceHandle: file.handle });
    expect(host.requests.some((r) => r.method === "pickFolder" || r.method === "createOutputFolder")).toBe(false);
    expect(host.requests.at(-1)?.method).toBe("closeOutputFolder");
    deck.dispose();
  });
  it("uses an explicitly chosen folder directly instead of the source folder", async () => {
    const { host, deck, file } = await setup();
    expect(await save(deck, file, true)).toBe(true);
    expect(host.requests.find((r) => r.method === "openOutputFolder")?.args).toEqual({ parentHandle: "chosen" });
    expect(host.requests.some((r) => r.method === "createOutputFolder")).toBe(false);
    deck.dispose();
  });
  it("asks for an actual destination when the new document has no source", async () => {
    const { host, deck } = await setup();
    expect(await save(deck)).toBe(true);
    expect(host.requests.some((r) => r.method === "pickFolder")).toBe(true);
    expect(host.requests.find((r) => r.method === "openOutputFolder")?.args).toEqual({ parentHandle: "chosen" });
    expect(host.requests.some((r) => r.method === "createOutputFolder")).toBe(false);
    deck.dispose();
  });
});
