// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import type { Deck, MessagingRecipient, FileHandleInfo } from "@deck/sdk";
import {
  Body,
  Button,
  Caption,
  CheckBox,
  ComboBox,
  InfoBar,
  SettingsExpander,
  TextBox,
  makeStyles,
  tokens,
} from "@deck/ui";
import { useEffect, useRef, useState } from "react";
import {
  evidence,
  evidenceKey,
  names,
  recipientLabel,
  resolveNames,
  validateDraft,
  type Draft,
  type Evidence,
} from "./logic.ts";
const useStyles = makeStyles({
  stack: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM, minWidth: 0 },
  row: { display: "flex", flexWrap: "wrap", gap: tokens.spacingHorizontalM, alignItems: "center" },
  field: { flex: "1 1 16rem", minWidth: 0 },
});
export function RecipientMapping({
  requested,
  bindings,
  recipients,
  onChange,
  disabled = false,
  explicit = false,
}: {
  requested: string[];
  bindings: Record<string, Evidence>;
  recipients: MessagingRecipient[];
  onChange: (value: Record<string, Evidence>) => void;
  disabled?: boolean;
  explicit?: boolean;
}) {
  const resolved = resolveNames(requested, bindings, recipients, explicit);
  return (
    <>
      {requested.map((name) => {
        const binding = bindings[name] ?? resolved.bindings[name];
        const matches = recipients.filter((r) => binding && evidenceKey(evidence(r)) === evidenceKey(binding));
        const match = matches.length === 1 ? matches[0] : undefined;
        const value = match?.recipientHandle ?? "";
        return (
          <ComboBox
            key={name}
            header={name}
            value={value}
            disabled={disabled}
            description={match ? recipientLabel(match) : "소속과 전체 계정 표시를 보고 선택하세요."}
            options={[
              { value: "", label: "실제 수신자 선택" },
              ...recipients.map((r) => ({ value: r.recipientHandle, label: recipientLabel(r) })),
            ]}
            onChange={(handle) => {
              const selected = recipients.find((r) => r.recipientHandle === handle);
              const next = Object.fromEntries(Object.entries(bindings).filter(([key]) => key !== name));
              if (selected) next[name] = evidence(selected);
              onChange(next);
            }}
          />
        );
      })}
    </>
  );
}
export function Editor({
  deck,
  draft,
  recipients,
  busy,
  onSave,
  onCancel,
}: {
  deck: Deck;
  draft: Draft;
  recipients: MessagingRecipient[];
  busy: boolean;
  onSave: (draft: Draft, repeat: { end: string; weekdays: number[]; excluded: string[] } | null) => void;
  onCancel: () => void;
}) {
  const s = useStyles(),
    [value, setValue] = useState(draft),
    [recipientText, setRecipientText] = useState(draft.recipientNames.join("\n")),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [repeatEnabled, setRepeat] = useState(false),
    [end, setEnd] = useState(draft.scheduledAt.slice(0, 10)),
    [weekdays, setWeekdays] = useState<number[]>([]),
    [excluded, setExcluded] = useState("");
  const patch = (change: Partial<Draft>) => setValue((old) => ({ ...old, ...change }));
  let requested: string[] = [];
  try {
    requested = names(recipientText);
  } catch {
    /* Empty input remains editable. */
  }
  const save = () => {
    try {
      const requested = names(recipientText),
        resolved = resolveNames(requested, value.bindings, recipients);
      const next = { ...value, recipientNames: requested, bindings: resolved.bindings };
      const issues = [...validateDraft(next), ...resolved.issues];
      if (issues.length) throw new Error(issues.join("\n"));
      setError("");
      onSave(
        next,
        repeatEnabled
          ? {
              end,
              weekdays,
              excluded: excluded
                .split(/[,;\r\n]/)
                .map((x) => x.trim())
                .filter(Boolean),
            }
          : null,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "입력을 확인하세요.");
    }
  };
  const attachmentState = useRef({ files: value.attachments, busy });
  attachmentState.current = { files: value.attachments, busy };
  const addAttachments = (files: FileHandleInfo[]) => {
    if (attachmentState.current.busy) {
      setError("현재 작업을 마친 뒤 첨부파일을 다시 놓아 주세요.");
      return;
    }
    if (!files.length) {
      setError("첨부할 파일을 놓아 주세요. 폴더는 첨부할 수 없어요.");
      return;
    }
    const merged = [
      ...new Map([...attachmentState.current.files, ...files].map((file) => [file.handle, file])).values(),
    ];
    if (merged.length > 10) {
      setError("첨부파일은 10개까지 추가해요. 기존 첨부를 정리한 뒤 다시 놓아 주세요.");
      return;
    }
    const added = merged.length - attachmentState.current.files.length;
    attachmentState.current.files = merged;
    patch({ attachments: merged, missingAttachments: [] });
    setError("");
    setNotice(
      added
        ? `${added}개 첨부파일을 추가했어요. ${files.map((file) => file.name).join(" · ")}`
        : "이미 추가한 첨부파일이에요.",
    );
  };
  const dropped = useRef(addAttachments);
  dropped.current = addAttachments;
  useEffect(() => deck.on("fs.dropped", ({ files }) => dropped.current(files)), [deck]);
  const attach = async () => {
    try {
      const files = await deck.fs.pickFiles({ multiple: true });
      if (!files.length) return;
      addAttachments(files);
    } catch {
      setError("첨부파일을 선택하지 못했습니다. 다시 선택하세요.");
    }
  };
  return (
    <div className={s.stack}>
      <Body>받는 사람과 한국시간 예약시각을 정하세요. 제목은 비워도 됩니다.</Body>
      {error && <InfoBar severity="error" message={error} />}
      {notice && <InfoBar severity="informational" message={notice} />}
      <TextBox
        header="받는 사람 · 이름 명단 붙여넣기"
        value={recipientText}
        onChange={setRecipientText}
        multiline
        rows={3}
        disabled={busy}
        description="쉼표 또는 줄바꿈으로 여러 명을 입력하세요. 동명이인은 아래 실제 계정을 선택합니다."
      />
      <SettingsExpander header="여러 명 선택" description="조직도에서 읽은 실제 계정을 선택해 명단에 넣습니다.">
        {recipients.map((r) => {
          const label = recipientLabel(r);
          return (
            <CheckBox
              key={r.recipientHandle}
              content={label}
              checked={requested.includes(label)}
              disabled={busy}
              onChange={(checked) => {
                const next = checked ? [...requested, label] : requested.filter((n) => n !== label);
                setRecipientText(next.join("\n"));
                patch({ bindings: { ...value.bindings, [label]: evidence(r) } });
              }}
            />
          );
        })}
      </SettingsExpander>
      <RecipientMapping
        requested={requested}
        bindings={value.bindings}
        recipients={recipients}
        onChange={(bindings) => patch({ bindings })}
        disabled={busy}
      />
      <div className={s.row}>
        <div className={s.field}>
          <TextBox
            header="예약 날짜 · YYYY-MM-DD"
            value={value.scheduledAt.slice(0, 10)}
            disabled={busy}
            onChange={(date) => patch({ scheduledAt: `${date}T${value.scheduledAt.slice(11)}` })}
          />
        </div>
        <div className={s.field}>
          <TextBox
            header="예약 시간 · HH:MM"
            value={value.scheduledAt.slice(11)}
            disabled={busy}
            onChange={(time) => patch({ scheduledAt: `${value.scheduledAt.slice(0, 10)}T${time}` })}
          />
        </div>
      </div>
      <TextBox header="제목 · 선택" value={value.title} disabled={busy} onChange={(title) => patch({ title })} />
      <TextBox
        header="내용"
        value={value.body}
        multiline
        rows={8}
        disabled={busy}
        onChange={(body) => patch({ body })}
      />
      <SettingsExpander header="첨부 · 반복 예약">
        <div className={s.stack}>
          <Caption>첨부는 파일을 놓거나 파일 선택으로 추가해요. 예약표의 전체 경로를 자동으로 열지 않아요.</Caption>
          <div className={s.row}>
            <Button
              disabled={busy}
              onClick={() => {
                void attach();
              }}
            >
              첨부파일 선택
            </Button>
            <Button
              disabled={busy || (!value.attachments.length && !value.missingAttachments.length)}
              onClick={() => patch({ attachments: [], missingAttachments: [] })}
            >
              첨부 모두 제거
            </Button>
          </div>
          {[
            ...value.attachments.map((f) => f.name),
            ...value.missingAttachments.map((name) => `${name} · 다시 선택 필요`),
          ].map((name, i) => (
            <Caption key={i}>{name}</Caption>
          ))}
          <CheckBox
            content="매주 반복 · 날짜마다 개별 초안 만들기"
            checked={repeatEnabled}
            disabled={busy || !!draft.workflow}
            onChange={setRepeat}
          />
          {repeatEnabled && (
            <>
              <Caption>공휴일을 자동 제외하지 않습니다. 제외일을 직접 입력하세요.</Caption>
              <div className={s.row}>
                {Array.from("월화수목금토일").map((label, n) => (
                  <CheckBox
                    key={n}
                    content={label}
                    checked={weekdays.includes(n)}
                    disabled={busy}
                    onChange={(checked) => setWeekdays((old) => (checked ? [...old, n] : old.filter((x) => x !== n)))}
                  />
                ))}
              </div>
              <TextBox header="반복 종료일" value={end} onChange={setEnd} disabled={busy} />
              <TextBox header="제외일 · 쉼표로 구분" value={excluded} onChange={setExcluded} disabled={busy} />
            </>
          )}
        </div>
      </SettingsExpander>
      <div className={s.row}>
        <Button appearance="primary" disabled={busy} onClick={save}>
          초안 저장 · 목록에서 검토
        </Button>
        <Button disabled={busy} onClick={onCancel}>
          취소
        </Button>
      </div>
    </div>
  );
}
