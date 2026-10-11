// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
// Module entry: connect to the shell first (BRG-003), then render inside DeckProvider (UI-004).
import { connect } from "@deck/sdk";
import { DeckProvider, EmptyState } from "@deck/ui";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";

const root = document.getElementById("root");
if (root !== null) {
  const view = createRoot(root);
  connect()
    .then((deck) =>
      view.render(
        <StrictMode>
          <DeckProvider deck={deck}>
            <App deck={deck} />
          </DeckProvider>
        </StrictMode>,
      ),
    )
    .catch(() =>
      view.render(
        <DeckProvider>
          <EmptyState title="도구를 시작하지 못했어요" description="덱에서 다시 열어 주세요." />
        </DeckProvider>,
      ),
    );
}
