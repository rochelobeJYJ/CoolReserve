// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
// @vitest-environment jsdom
import { connect, type FileHandleInfo } from "@deck/sdk";
import { createMockHost } from "@deck/sdk/testing";
import { DeckProvider } from "@deck/ui";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "./Editor.tsx";
import { createDraft, evidence } from "./logic.ts";

afterEach(cleanup);
const file = (handle: string, name: string): FileHandleInfo => ({ handle, name, ext: "txt", size: 10, modifiedAt: 0 });
async function setup(missing: string[]) {
  const host = createMockHost({
    module: { id: "coolreserve", version: "0.2.1" },
    granted: ["fs"],
    handlers: { "fs.pickFiles": () => [] },
  });
  const deck = await connect({ window: host.window });
  const recipient = {
    recipientHandle: "r1",
    displayName: "합성교사가",
    department: "합성부서",
    accountLabel: "합성계정",
  };
  const onSave = vi.fn();
  render(
    <DeckProvider>
      <Editor
        deck={deck}
        draft={createDraft({
          recipientNames: [recipient.displayName],
          bindings: { [recipient.displayName]: evidence(recipient) },
          body: "합성 안내",
          missingAttachments: missing,
        })}
        recipients={[recipient]}
        busy={false}
        onSave={onSave}
        onCancel={() => undefined}
      />
    </DeckProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "첨부 · 반복 예약" }));
  return { host, deck, onSave };
}
describe("restored attachment verification", () => {
  it("keeps the other attachment missing after partial and unrelated reselection", async () => {
    const { host, deck, onSave } = await setup(["합성A.txt", "합성B.txt"]);
    await act(async () => host.emit("fs.dropped", { files: [file("a", "합성A.txt")] }));
    expect(screen.queryByText("합성A.txt · 다시 선택 필요")).toBeNull();
    expect(screen.getByText("합성B.txt · 다시 선택 필요")).toBeTruthy();
    await act(async () => host.emit("fs.dropped", { files: [file("unrelated", "합성C.txt"), file("a", "합성A.txt")] }));
    fireEvent.click(screen.getByRole("button", { name: "초안 저장 · 목록에서 검토" }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("저장한 첨부파일을 다시 선택하거나 첨부 목록에서 제거하세요.")).toBeTruthy();
    await act(async () => host.emit("fs.dropped", { files: [file("b", "합성B.TXT")] }));
    fireEvent.click(screen.getByRole("button", { name: "초안 저장 · 목록에서 검토" }));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ missingAttachments: [], attachments: expect.any(Array) }),
      null,
    );
    expect(onSave.mock.calls[0]?.[0].attachments.map((value: FileHandleInfo) => value.handle)).toEqual([
      "a",
      "unrelated",
      "b",
    ]);
    deck.dispose();
  });
  it("requires explicit removal when a table cannot identify the original filename", async () => {
    const { host, deck, onSave } = await setup(["예약표의 첨부파일"]);
    await act(async () => host.emit("fs.dropped", { files: [file("new", "합성 새 첨부.txt")] }));
    expect(screen.getByText("예약표의 첨부파일 · 다시 선택 필요")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "첨부 모두 제거" }));
    fireEvent.click(screen.getByRole("button", { name: "초안 저장 · 목록에서 검토" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ missingAttachments: [], attachments: [] }), null);
    deck.dispose();
  });
});
