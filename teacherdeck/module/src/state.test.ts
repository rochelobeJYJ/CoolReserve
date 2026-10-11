// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import { connect } from "@deck/sdk";
import { createMockHost } from "@deck/sdk/testing";
import { describe, expect, it } from "vitest";
import { createDraft } from "./logic.ts";
import { backup, initialState, loadState, readBackup, saveState, STATE_KEY } from "./state.ts";
function setup() {
  const data = new Map<string, unknown>();
  let fail = false;
  const host = createMockHost({
    module: { id: "coolreserve", version: "0.1.0" },
    granted: ["storage"],
    handlers: {
      "storage.get": (args) => data.get((args as { key: string }).key) ?? null,
      "storage.keys": () => [...data.keys()],
      "storage.delete": (args) => {
        data.delete((args as { key: string }).key);
        return null;
      },
      "storage.set": (args) => {
        const { key, value } = args as { key: string; value: unknown };
        expect(new TextEncoder().encode(JSON.stringify(value)).length).toBeLessThan(240 * 1024);
        if (fail && key === STATE_KEY) throw new Error("SYNTHETIC_WRITE_FAILURE");
        data.set(key, structuredClone(value));
        return null;
      },
    },
  });
  return {
    data,
    host,
    fail: () => {
      fail = true;
    },
  };
}
describe("durable module drafts", () => {
  it("round trips 500 messages in bounded chunks and preserves optional titles, archives and recipes", async () => {
    const { host } = setup(),
      deck = await connect({ window: host.window }),
      state = initialState();
    state.drafts = Array.from({ length: 500 }, (_, n) =>
      createDraft({ body: "합성 본문".repeat(50), title: "", recipientNames: ["가상교사"], archived: n % 2 === 0 }),
    );
    await saveState(deck, state);
    expect(await loadState(deck)).toEqual(state);
    expect(readBackup(backup(state))).toEqual(state);
    deck.dispose();
  });
  it("does not persist attachment handles and requires re-selection after restart", async () => {
    const { host } = setup(),
      deck = await connect({ window: host.window }),
      state = initialState();
    state.drafts = [
      createDraft({
        attachments: [{ handle: "opaque-file", name: "합성첨부.txt", ext: "txt", size: 10, modifiedAt: 0 }],
      }),
    ];
    await saveState(deck, state);
    const restored = await loadState(deck);
    expect(restored.drafts[0]?.attachments).toEqual([]);
    expect(restored.drafts[0]?.missingAttachments).toEqual(["합성첨부.txt"]);
    expect(new TextDecoder().decode(backup(state))).not.toContain("opaque-file");
    deck.dispose();
  });
  it("keeps prior generation after commit failure and preserves damaged data until explicit recovery", async () => {
    const { host, data, fail } = setup(),
      deck = await connect({ window: host.window }),
      state = initialState();
    await saveState(deck, state);
    fail();
    await expect(saveState(deck, { ...state, drafts: [createDraft({ body: "합성 변경" })] })).rejects.toThrow();
    expect(await loadState(deck)).toEqual(state);
    data.set(STATE_KEY, { version: 0 });
    await expect(loadState(deck)).rejects.toThrow();
    await expect(saveState(deck, state)).rejects.toThrow();
    expect(data.get(STATE_KEY)).toEqual({ version: 0 });
    deck.dispose();
  });
  it("converts interrupted submission to uncertain and refuses invalid backup records", async () => {
    const { host } = setup(),
      deck = await connect({ window: host.window }),
      state = initialState();
    state.drafts = [createDraft({ state: "submitting", retryLocked: true })];
    await saveState(deck, state);
    expect((await loadState(deck)).drafts[0]?.state).toBe("uncertain");
    expect(() => readBackup(new TextEncoder().encode('{"version":1,"drafts":[{}],"recipes":[]}'))).toThrow();
    deck.dispose();
  });
});
