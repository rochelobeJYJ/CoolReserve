// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
// @vitest-environment jsdom
import { webcrypto } from "node:crypto";
import { connect, DeckCallError, type MessagingPlanArgs, type ScheduledMessage } from "@deck/sdk";
import { createMockHost, type MockHandler } from "@deck/sdk/testing";
import { DeckProvider } from "@deck/ui";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App.tsx";
import { createDraft, evidence, kstNow } from "./logic.ts";
import { initialState, loadState, STATE_KEY } from "./state.ts";
const accounts = [
  { recipientHandle: "r1", displayName: "가상교사가", department: "합성부서1", accountLabel: "계정1" },
  { recipientHandle: "r2", displayName: "가상교사가", department: "합성부서2", accountLabel: "계정2" },
];
beforeEach(() => {
  vi.stubGlobal("crypto", webcrypto);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
async function setup(overrides: Record<string, MockHandler> = {}) {
  const first = accounts[0];
  if (!first) throw new Error("TEST_SETUP");
  const state = initialState();
  state.drafts = [
    createDraft({
      id: "synthetic-message",
      title: "",
      body: "합성 본문",
      recipientNames: ["가상교사가"],
      bindings: { 가상교사가: evidence(first) },
      scheduledAt: kstNow(new Date(Date.now() + 3600000)),
    }),
  ];
  const data = new Map<string, unknown>([[STATE_KEY, state]]);
  let messages: ScheduledMessage[] = [];
  const host = createMockHost({
    module: { id: "coolreserve", version: "0.1.0" },
    granted: ["storage", "messaging", "fs"],
    handlers: {
      "storage.get": (args) => data.get((args as { key: string }).key) ?? null,
      "storage.set": (args) => {
        const { key, value } = args as { key: string; value: unknown };
        data.set(key, structuredClone(value));
        return null;
      },
      "storage.keys": () => [...data.keys()],
      "storage.delete": (args) => {
        data.delete((args as { key: string }).key);
        return null;
      },
      "messaging.connect": () => ({
        connectionHandle: "connection",
        ready: true,
        versionSupport: "supported",
        capabilities: ["multi-recipient"],
      }),
      "messaging.recipients": () => accounts,
      "messaging.plan": (args) => {
        messages = (args as MessagingPlanArgs).messages;
        return {
          planHandle: "plan",
          expiresAt: "2099-01-01T00:00:00Z",
          issues: [],
          preview: messages.map((message) => ({
            message,
            recipientLabels: ["가상교사가 · 합성부서1 · 계정1"],
            attachmentLabels: [],
            retryLocked: false,
          })),
        };
      },
      "messaging.start": () => ({ runHandle: "run" }),
      "messaging.status": () => ({
        runHandle: "run",
        active: false,
        mode: "register",
        stage: "done",
        progress: messages.length,
        total: messages.length,
        items: messages.map((message) => ({
          messageId: message.id,
          state: "registration-requested",
          retryLocked: true,
        })),
        issue: null,
      }),
      ...overrides,
    },
  });
  const deck = await connect({ window: host.window }),
    view = render(
      <DeckProvider>
        <App deck={deck} />
      </DeckProvider>,
    );
  await waitFor(() => expect(screen.getByRole("button", { name: "새 메시지" }).hasAttribute("disabled")).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "조직도 계정 불러오기" }));
  await screen.findByText("현재 조직도에서 2개 계정을 읽었습니다. 표시 필터에 따라 일부 계정이 빠질 수 있습니다.");
  return { host, deck, view };
}
async function review() {
  fireEvent.click(screen.getByRole("checkbox", { name: "합성 본문" }));
  fireEvent.click(screen.getByRole("button", { name: "선택한 1건 내용 검토" }));
  await screen.findByRole("button", { name: "1건 한 번에 등록 · 덱 확인창" });
}
describe("six-method messaging UI", () => {
  it("shows immutable review before start, preserves blank title and locks submitted messages", async () => {
    const { host, deck, view } = await setup();
    await review();
    expect(host.requests.some((r) => r.method === "start")).toBe(false);
    const request = host.requests.find((r) => r.method === "plan");
    expect((request?.args as MessagingPlanArgs).messages[0]?.title).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "1건 한 번에 등록 · 덱 확인창" }));
    await screen.findByText("등록 요청 완료 · 자동 재등록 잠금");
    await waitFor(async () => expect((await loadState(deck)).drafts[0]?.retryLocked).toBe(true));
    const start = host.requests.find((r) => r.method === "start");
    expect(start?.args).toEqual({ planHandle: "plan", confirmation: null });
    view.unmount();
    deck.dispose();
  });
  it("keeps unsubmitted drafts when trusted confirmation is cancelled", async () => {
    const { host, deck, view } = await setup({
      "messaging.start": () => {
        throw new DeckCallError("PERMISSION_DENIED", "synthetic");
      },
    });
    await review();
    fireEvent.click(screen.getByRole("button", { name: "1건 한 번에 등록 · 덱 확인창" }));
    await screen.findByText("덱의 예약 등록 확인을 취소했습니다. 초안은 유지했습니다.");
    expect((await loadState(deck)).drafts[0]?.retryLocked).toBe(false);
    expect(host.requests.some((r) => r.method === "status")).toBe(false);
    view.unmount();
    deck.dispose();
  });
  it("keeps draft and shows recipient mismatch without starting an invalid plan", async () => {
    const { host, deck, view } = await setup({
      "messaging.plan": () => ({
        planHandle: "blocked",
        expiresAt: "2099-01-01T00:00:00Z",
        issues: [{ messageId: "synthetic-message", code: "recipient-mismatch" }],
        preview: [],
      }),
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "합성 본문" }));
    fireEvent.click(screen.getByRole("button", { name: "선택한 1건 내용 검토" }));
    await screen.findByText("수신자 계정이 바뀌었거나 확인되지 않았습니다. 계정을 다시 불러오세요.");
    expect(host.requests.some((r) => r.method === "start")).toBe(false);
    expect((await loadState(deck)).drafts).toHaveLength(1);
    view.unmount();
    deck.dispose();
  });
});
