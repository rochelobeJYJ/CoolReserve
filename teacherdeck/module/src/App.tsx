// SPDX-License-Identifier: GPL-3.0-only
// Additional terms: see LICENSE-ADDITIONAL-TERMS
import type {
  Deck,
  MessagingConnection,
  MessagingRecipient,
  MessagingPlan,
  MessagingMode,
  MessagingStatus,
  MessagingIssueCode,
} from "@deck/sdk";
import {
  Body,
  Button,
  Caption,
  CheckBox,
  ComboBox,
  EmptyState,
  InfoBar,
  ListView,
  NumberBox,
  PageHeader,
  ProgressBar,
  SettingsExpander,
  TextBox,
  ToolLayout,
  makeStyles,
  tokens,
} from "@deck/ui";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Editor, RecipientMapping } from "./Editor.tsx";
import {
  batchDrafts,
  content,
  createDraft,
  kstNow,
  names,
  recipientLabel,
  repeat,
  resolveNames,
  scheduledMessage,
  stateLabel,
  validateDraft,
  type Draft,
} from "./logic.ts";
import { applyWorkflow, detectHeader, general, workflowReview, type Recipe, type WorkflowReview } from "./workflows.ts";
import { backup, initialState, loadState, readBackup, saveState, type State } from "./state.ts";
import { historyCsv, parseText, readBytes, readTable, saveFile, saveTemplate } from "./io.ts";
const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalL, padding: tokens.spacingHorizontalXL },
  stack: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM, minWidth: 0 },
  row: { display: "flex", flexWrap: "wrap", gap: tokens.spacingHorizontalM, alignItems: "center" },
  field: { flex: "1 1 16rem", minWidth: 0 },
  message: { whiteSpace: "pre-wrap", overflowWrap: "anywhere" },
});
const issueLabel: Record<MessagingIssueCode, string> = {
  "invalid-message": "예약시간·본문·입력 범위를 확인하세요.",
  "recipient-mismatch": "수신자 계정이 바뀌었거나 확인되지 않았습니다. 계정을 다시 불러오세요.",
  "attachment-changed": "첨부파일이 바뀌었거나 열리지 않습니다. 다시 선택하세요.",
  "retry-locked": "이미 등록을 시도한 메시지입니다. 쿨메신저에서 등록 여부를 확인하세요.",
  "unsupported-feature": "현재 쿨메신저 연결에서 이 기능을 지원하지 않습니다.",
  "connection-changed": "로그인 계정 또는 작성창 연결이 바뀌었습니다. 다시 연결하세요.",
  "provider-failed": "쿨메신저 처리가 멈췄습니다. 로그인·조직도·작성창 상태를 확인하세요.",
};
const stages: Record<MessagingStatus["stage"], string> = {
  idle: "대기",
  validating: "검증 중",
  reading: "작성창 읽기",
  preparing: "자동 입력",
  verifying: "내용 대조",
  submitting: "예약 등록",
  "checking-result": "등록 결과 대조",
  done: "처리 완료",
  stopped: "중지",
  failed: "중단",
  "needs-review": "등록 결과 확인 필요",
};
type View = "jobs" | "editor" | "batch" | "workflow" | "archives" | "history";
function WorkflowLayout({
  title,
  input,
  options,
  preview,
  run,
  result,
}: { title: string } & Record<
  "input" | "options" | "preview" | "run" | "result",
  { title: string; children: ReactNode }
>) {
  return (
    <ToolLayout title={title}>
      {(
        [
          ["input", input],
          ["options", options],
          ["preview", preview],
          ["run", run],
          ["result", result],
        ] as const
      ).map(([step, section]) => (
        <ToolLayout.Section key={step} step={step} title={section.title}>
          {section.children}
        </ToolLayout.Section>
      ))}
    </ToolLayout>
  );
}
export function App({ deck }: { deck: Deck }) {
  const s = useStyles(),
    [state, setState] = useState<State>(initialState),
    stateRef = useRef(state);
  const [loaded, setLoaded] = useState(false),
    [corrupt, setCorrupt] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false),
    lock = useRef(false),
    [view, setView] = useState<View>("jobs"),
    [connection, setConnection] = useState<MessagingConnection | null>(null),
    [selfHelp, setSelfHelp] = useState(false),
    [recipients, setRecipients] = useState<MessagingRecipient[]>([]);
  const [selected, setSelected] = useState<string[]>([]),
    [filter, setFilter] = useState("all"),
    [page, setPage] = useState(0),
    [search, setSearch] = useState(""),
    [editor, setEditor] = useState<Draft | null>(null);
  const [batch, setBatch] = useState<Draft[]>([]),
    [paste, setPaste] = useState(""),
    [batchPage, setBatchPage] = useState(0),
    [mode, setMode] = useState<MessagingMode>("register"),
    [plan, setPlan] = useState<MessagingPlan | null>(null),
    [plannedIds, setPlannedIds] = useState<string[]>([]),
    [run, setRun] = useState<MessagingStatus | null>(null),
    [runHandle, setRunHandle] = useState(""),
    [pollFailure, setPollFailure] = useState(false);
  const [tables, setTables] = useState<{ name: string; values: string[][] }[]>([]),
    [sheet, setSheet] = useState(""),
    [source, setSource] = useState(""),
    [recipe, setRecipe] = useState<Recipe>(structuredClone(general)),
    [wfStep, setWfStep] = useState(1),
    [from, setFrom] = useState(kstNow().slice(0, 10)),
    [to, setTo] = useState(kstNow(new Date(Date.now() + 30 * 86400000)).slice(0, 10)),
    [review, setReview] = useState<WorkflowReview | null>(null),
    [reviewDirty, setReviewDirty] = useState(false),
    [previewPage, setPreviewPage] = useState(0);
  const abortRef = useRef<AbortController | null>(null),
    blocked = busy || !!run?.active || corrupt || !loaded;
  useEffect(() => {
    let alive = true;
    void loadState(deck)
      .then((value) => {
        if (alive) {
          stateRef.current = value;
          setState(value);
          setLoaded(true);
        }
      })
      .catch(() => {
        if (alive) {
          setLoaded(true);
          setCorrupt(true);
          setError("저장한 초안을 읽지 못했습니다. 기록을 보존하고 백업 복구 또는 새 작업 확인을 기다립니다.");
        }
      });
    return () => {
      alive = false;
      abortRef.current?.abort();
    };
  }, [deck]);
  const persist = async (next: State, recover = false) => {
    await saveState(deck, next, recover);
    stateRef.current = next;
    setState(next);
  };
  const task = async (
    action: () => Promise<void>,
    message = "처리하지 못했습니다. 입력과 연결 상태를 확인한 뒤 다시 시도하세요.",
  ) => {
    if (lock.current || run?.active) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch {
      setError(message);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  const localTask = async (action: () => Promise<void>) => {
    if (lock.current || run?.active) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : "입력을 확인하세요.");
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  const connect = () =>
    task(async () => {
      let next: MessagingConnection;
      try {
        next = await deck.messaging.connect({ provider: "gentoo" });
      } catch (e) {
        const message = e instanceof Error ? e.message : "";
        if (
          message ===
          "처음 연결할 때 쿨메신저에서 본인 항목을 선택하고 나에게 보내기를 켠 본인 작성창을 준비한 뒤 연결해 주세요."
        ) {
          setSelfHelp(true);
          setError(message);
          return;
        }
        throw e;
      }
      const accounts = await deck.messaging.recipients({ connectionHandle: next.connectionHandle });
      setConnection(next);
      setSelfHelp(false);
      setRecipients(accounts);
      setPlan(null);
      setNotice(
        `현재 조직도에서 ${accounts.length}개 계정을 읽었습니다. 표시 필터에 따라 일부 계정이 빠질 수 있습니다.`,
      );
    }, "쿨메신저 계정을 불러오지 못했습니다. 로그인하고 조직도 창을 연 뒤 다시 시도하세요.");
  const invalidate = () => {
    setPlan(null);
    setReview(null);
  };
  const changeRecipe = (changes: Partial<Recipe>) => {
    setRecipe((old) => ({ ...old, ...changes }));
    setReviewDirty(true);
  };
  const saveDrafts = async (incoming: Draft[], replaceIds: string[] = []) => {
    const next = [...stateRef.current.drafts.filter((d) => !replaceIds.includes(d.id)), ...incoming];
    if (next.filter((d) => !d.archived).length > 500)
      throw new Error("기본 목록은 500건까지입니다. 지난 메시지를 보관한 뒤 다시 저장하세요.");
    if (
      incoming.some(
        (d, i) =>
          incoming.slice(0, i).some((other) => content(other) === content(d)) ||
          stateRef.current.drafts.some((old) => !replaceIds.includes(old.id) && content(old) === content(d)),
      )
    )
      throw new Error("같은 수신자·시각·내용의 초안이 이미 있습니다. 전체를 다시 검토하세요.");
    await persist({ ...stateRef.current, drafts: next });
    setView("jobs");
    setSelected(incoming.filter((d) => !d.retryLocked).map((d) => d.id));
    setPage(0);
    invalidate();
    setNotice(`${incoming.length}건 초안을 저장했습니다. 아직 예약 등록을 하지 않았습니다.`);
  };
  const saveEditor = (draft: Draft, repetition: { end: string; weekdays: number[]; excluded: string[] } | null) => {
    void localTask(async () => {
      const prior = stateRef.current.drafts.find((d) => d.id === draft.id);
      if (prior?.retryLocked) throw new Error("등록 시도 기록이 있는 메시지는 수정할 수 없습니다.");
      const incoming = repetition
        ? repeat(draft, repetition.end, repetition.weekdays, repetition.excluded)
        : [{ ...draft, revision: (prior?.revision ?? 0) + 1 }];
      const issues = incoming.flatMap((d) => validateDraft(d));
      if (issues.length) throw new Error(issues[0]);
      if (!prior && batch.some((row) => row.id === draft.id)) {
        setBatch((old) => [...old.filter((row) => row.id !== draft.id), ...incoming]);
        setView("batch");
        setEditor(null);
        return;
      }
      await saveDrafts(incoming, prior ? [prior.id] : []);
      setEditor(null);
    });
  };
  const readInput = (kind: "batch" | "workflow") =>
    task(async () => {
      const files = await deck.fs.pickFiles({
        multiple: false,
        filters: [{ name: "엑셀·CSV", extensions: ["xlsx", "csv"] }],
      });
      const file = files[0];
      if (!file) return;
      const abort = new AbortController();
      abortRef.current = abort;
      const data = await readTable(deck, file, abort.signal);
      setTables(data);
      setSheet(data[0]?.name ?? "");
      setSource(`${file.name} · ${data[0]?.name ?? ""}`);
      if (kind === "batch") {
        setBatch(batchDrafts(data[0]?.values ?? []));
        setBatchPage(0);
        setView("batch");
      } else {
        const values = data[0]?.values ?? [];
        setRecipe((old) => ({ ...old, headerRows: detectHeader(values, old) }));
        setWfStep(1);
        setView("workflow");
        setReview(null);
      }
    }, "자료를 읽지 못했습니다. 10MiB 이하 XLSX·CSV와 510행·128열 범위를 확인하세요.");
  const preview = () =>
    task(async () => {
      if (!connection) {
        setError("조직도 계정을 먼저 불러오세요.");
        return;
      }
      const drafts = stateRef.current.drafts.filter((d) => selected.includes(d.id) && !d.archived);
      if (!drafts.length) {
        setError("검토할 미등록 메시지를 선택하세요.");
        return;
      }
      if (drafts.some((d) => d.attachments.length) && !connection.capabilities.includes("attachments")) {
        setError(
          "연결된 쿨메신저에서 첨부 등록을 확인할 수 없어요. 초안의 첨부는 보존했습니다. 첨부 없이 등록하거나 지원되는 연결을 준비하세요.",
        );
        return;
      }
      const local = drafts.map((d) => scheduledMessage(d, recipients));
      const issues = local.flatMap((d) => d.issues);
      if (issues.length) {
        setError(issues.join("\n"));
        return;
      }
      if ((mode === "inspect" || mode === "prepare") && drafts.length !== 1) {
        setError("작성창 대조·자동 입력은 한 건만 선택하세요.");
        return;
      }
      const next = await deck.messaging.plan({
        connectionHandle: connection.connectionHandle,
        messages: local.map((d) => d.message),
        mode,
        resultConfirmation: "manual",
      });
      setPlan(next);
      setPlannedIds(drafts.map((d) => d.id));
      setPreviewPage(0);
      if (next.issues.some((i) => i.code === "retry-locked") || next.preview.some((p) => p.retryLocked)) {
        const locked = new Set([
          ...next.preview.filter((p) => p.retryLocked).map((p) => p.message.id),
          ...next.issues.filter((i) => i.code === "retry-locked").flatMap((i) => (i.messageId ? [i.messageId] : [])),
        ]);
        await persist({
          ...stateRef.current,
          drafts: stateRef.current.drafts.map((d) =>
            locked.has(d.id) ? { ...d, retryLocked: true, state: "uncertain" } : d,
          ),
        });
      }
    }, "예약 검토를 준비하지 못했습니다. 계정·첨부·예약시각을 다시 확인하세요.");
  const start = () =>
    task(async () => {
      if (!plan || plan.issues.length || plan.preview.some((p) => p.retryLocked)) return;
      const pending = stateRef.current.drafts;
      // Persist the submission boundary first. Lost start responses cannot expose an automatic retry.
      if (mode === "register")
        await persist({
          ...stateRef.current,
          drafts: pending.map((d) =>
            plannedIds.includes(d.id) ? { ...d, state: "submitting", retryLocked: true } : d,
          ),
        });
      try {
        const handle = await deck.messaging.start({ planHandle: plan.planHandle, confirmation: null });
        setRunHandle(handle.runHandle);
        setRun({
          runHandle: handle.runHandle,
          active: true,
          mode,
          stage: "validating",
          progress: 0,
          total: plannedIds.length,
          items: [],
          issue: null,
        });
        setPollFailure(false);
        setPlan(null);
      } catch (e) {
        const code = typeof e === "object" && e && "code" in e ? String(e.code) : "";
        if (mode === "register" && ["PERMISSION_DENIED", "CANCELLED"].includes(code)) {
          await persist({ ...stateRef.current, drafts: pending });
          setNotice("덱의 예약 등록 확인을 취소했습니다. 초안은 유지했습니다.");
          return;
        } else if (mode === "register")
          await persist({
            ...stateRef.current,
            drafts: stateRef.current.drafts.map((d) =>
              plannedIds.includes(d.id) ? { ...d, state: "uncertain", retryLocked: true } : d,
            ),
          });
        throw e;
      }
    }, "실행하지 못했거나 시작 여부를 확인하지 못했습니다. 등록 여부 불명인 항목은 쿨메신저에서 직접 확인하세요.");
  useEffect(() => {
    if (!runHandle || pollFailure) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const status = await deck.messaging.status({ runHandle });
        if (!alive) return;
        setRun(status);
        const byId = new Map(status.items.map((item) => [item.messageId, item]));
        const next = {
          ...stateRef.current,
          drafts: stateRef.current.drafts.map((d) => {
            const item = byId.get(d.id);
            return item && (status.mode === "register" || status.mode === "prepare" || item.retryLocked)
              ? { ...d, state: item.state, retryLocked: item.retryLocked }
              : d;
          }),
        };
        if (JSON.stringify(next) !== JSON.stringify(stateRef.current)) await persist(next);
        if (alive && status.active)
          timer = setTimeout(() => {
            void poll();
          }, 900);
      } catch {
        if (alive) {
          setPollFailure(true);
          setError(
            "실행 상태 또는 기록 저장을 확인하지 못했습니다. 중지를 요청하고 쿨메신저에서 등록 여부를 확인하세요.",
          );
        }
      }
    };
    void poll();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [deck, runHandle, pollFailure]);
  const cancel = async () => {
    if (!runHandle) return;
    try {
      await deck.messaging.cancel({ runHandle });
      setNotice(
        "중지를 요청했습니다. 현재 Windows 호출이 끝난 뒤 남은 처리를 멈춥니다. 이미 접수된 예약은 취소되지 않습니다.",
      );
    } catch {
      setError("중지 요청을 확인하지 못했습니다. F8과 쿨메신저 화면을 확인하세요.");
    }
  };
  const wfValues = tables.find((t) => t.name === sheet)?.values ?? [];
  const workflowPreview = () =>
    localTask(async () => {
      const result = await workflowReview(wfValues, recipe, from, to, source, recipients, stateRef.current.drafts);
      setReview(result);
      setReviewDirty(false);
      setWfStep(3);
      setPreviewPage(0);
    });
  const shown = state.drafts
    .filter((d) =>
      view === "archives"
        ? d.archived
        : view === "history"
          ? d.retryLocked || ["prepared", "failed"].includes(d.state)
          : !d.archived && (filter === "all" || (filter === "draft" ? !d.retryLocked : d.retryLocked)),
    )
    .filter((d) => !search || `${d.title} ${d.body} ${d.recipientNames.join(" ")}`.includes(search))
    .sort((a, b) => a.scheduledAt.localeCompare(b.scheduledAt));
  const pageCount = Math.max(1, Math.ceil(shown.length / 50)),
    safePage = Math.min(page, pageCount - 1),
    eligible = shown.filter((d) => !d.retryLocked && !d.archived);
  const archive = (ids: string[], archived: boolean) =>
    localTask(async () => {
      const next = {
        ...stateRef.current,
        drafts: stateRef.current.drafts.map((d) => (ids.includes(d.id) ? { ...d, archived } : d)),
      };
      if (next.drafts.filter((d) => !d.archived).length > 500) throw new Error("복원하면 기본 목록 500건을 넘습니다.");
      await persist(next);
      setSelected([]);
      invalidate();
    });
  const remove = () =>
    localTask(async () => {
      if (stateRef.current.drafts.some((d) => selected.includes(d.id) && d.retryLocked))
        throw new Error("등록 기록은 삭제하지 않고 보관함으로 옮겨 주세요.");
      await persist({ ...stateRef.current, drafts: stateRef.current.drafts.filter((d) => !selected.includes(d.id)) });
      setSelected([]);
      invalidate();
    });
  const exportBackup = () =>
    task(async () => {
      if (await saveFile(deck, "쿨예약실_백업.json", backup(stateRef.current)))
        setNotice("초안·보관·양식을 백업했습니다. 첨부파일 내용은 포함하지 않습니다.");
    }, "백업을 저장하지 못했습니다. 다른 폴더로 다시 저장하세요.");
  const importBackup = () =>
    task(async () => {
      const files = await deck.fs.pickFiles({
        multiple: false,
        filters: [{ name: "쿨 예약실 백업", extensions: ["json"] }],
      });
      if (!files[0]) return;
      const next = readBackup(await readBytes(deck, files[0], new AbortController().signal));
      await persist(next, corrupt);
      setCorrupt(false);
      invalidate();
      setNotice("백업을 복구했습니다. 첨부파일을 다시 선택하고 조직도 계정을 불러오세요.");
    }, "백업을 복구하지 못했습니다. 원본 저장 기록을 보존했습니다.");
  const navigation = (
    <div className={s.row}>
      <Button
        appearance="primary"
        disabled={blocked}
        onClick={() => {
          setEditor(createDraft());
          setView("editor");
          invalidate();
        }}
      >
        새 메시지
      </Button>
      <Button
        disabled={blocked}
        onClick={() => {
          setBatch([createDraft()]);
          setView("batch");
          setBatchPage(0);
          invalidate();
        }}
      >
        여러 메시지 입력
      </Button>
      <Button
        disabled={blocked}
        onClick={() => {
          void readInput("batch");
        }}
      >
        예약표 가져오기
      </Button>
      <Button
        disabled={blocked}
        onClick={() => {
          setView("batch");
          setBatch([]);
          setPaste("");
          invalidate();
        }}
      >
        표 붙여넣기
      </Button>
      <Button
        disabled={blocked}
        onClick={() => {
          setView("workflow");
          setWfStep(1);
          invalidate();
        }}
      >
        자료로 메시지 만들기
      </Button>
      <Button
        disabled={blocked}
        onClick={() => {
          void task(async () => {
            await saveTemplate(deck, "batch");
          });
        }}
      >
        엑셀 양식 받기
      </Button>
    </div>
  );
  const messagePreview = (d: Draft) => (
    <div className={s.stack}>
      <Caption>
        {d.scheduledAt.replace("T", " ")} · {d.recipientNames.join(" · ")}
      </Caption>
      <Body>{d.title || "제목 없음"}</Body>
      <div className={s.message}>
        <Body>{d.body}</Body>
      </div>
      {Object.values(d.bindings).map((r, i) => (
        <Caption key={i}>실제 수신자: {recipientLabel(r)}</Caption>
      ))}
      {d.attachments.map((f) => (
        <Caption key={f.handle}>첨부: {f.name}</Caption>
      ))}
    </div>
  );
  return (
    <div className={s.page}>
      <PageHeader title="쿨 예약실" description="예약 메시지를 만들고 실제 수신자·내용·시간을 검토합니다." />
      <div className={s.row}>
        <Button
          disabled={busy || !!run?.active}
          onClick={() => {
            setView("jobs");
            setPage(0);
          }}
        >
          예약 메시지
        </Button>
        <Button
          disabled={busy || !!run?.active}
          onClick={() => {
            setView("archives");
            setPage(0);
          }}
        >
          보관함 {state.drafts.filter((d) => d.archived).length}
        </Button>
        <Button
          disabled={busy || !!run?.active}
          onClick={() => {
            setView("history");
            setPage(0);
          }}
        >
          실행 기록
        </Button>
        <Button disabled={blocked} onClick={exportBackup}>
          백업 저장
        </Button>
        <Button disabled={busy || !!run?.active} onClick={importBackup}>
          백업 복구
        </Button>
      </div>
      {error && <InfoBar severity="error" message={error} />}
      {notice && <InfoBar severity="informational" message={notice} />}
      {corrupt && (
        <InfoBar
          severity="warning"
          message="손상된 저장 기록을 보존하고 있습니다. 백업 복구는 확인한 파일로만 진행하세요."
        />
      )}
      <div className={s.row}>
        <Caption>
          {connection
            ? `${connection.ready ? "연결 확인됨" : "연결 확인 필요"} · ${recipients.length}개 계정`
            : "쿨메신저에 로그인한 뒤 조직도 계정을 불러오세요."}
        </Caption>
        <Button
          disabled={blocked}
          onClick={() => {
            void connect();
          }}
        >
          조직도 계정 불러오기
        </Button>
      </div>
      <SettingsExpander
        key={`${connection?.connectionHandle ?? "initial"}-${selfHelp}`}
        header="연결 도움말"
        defaultExpanded={selfHelp}
      >
        <div className={s.stack}>
          <Caption>
            계정이 빠지면 조직도 표시 필터를 전체로 바꾸세요. 현재 읽은 계정은 전체 명부를 보장하지 않습니다.
          </Caption>
          <Caption>
            본인 확인 안내가 나오면 쿨메신저의 본인 항목에서 ‘나에게 보내기’를 켠 본인 작성창을 준비한 뒤 다시
            연결하세요.
          </Caption>
          {connection && !connection.ready && (
            <InfoBar
              severity="warning"
              message="현재 연결의 화면 또는 계정 정보를 확인하지 못했습니다. 쿨메신저를 확인하고 다시 연결하세요."
            />
          )}
        </div>
      </SettingsExpander>
      {busy && <ProgressBar header="처리 중" />}
      {busy && abortRef.current && <Button onClick={() => abortRef.current?.abort()}>자료 읽기 중지</Button>}
      {run && (
        <div className={s.stack}>
          <InfoBar
            severity={run.active ? "warning" : run.issue ? "error" : "informational"}
            title={`${run.progress}/${run.total}건 · ${stages[run.stage]}`}
            message={
              run.active && ["register", "prepare"].includes(run.mode)
                ? "지금은 PC 사용을 멈춰 주세요. 마우스·키보드, 화면 잠금·절전은 처리 종료 후 사용하세요."
                : run.active
                  ? "현재 작업은 입력하거나 등록하지 않습니다."
                  : run.mode === "register" && run.stage === "done"
                    ? "등록 요청을 마쳤습니다. 도착 여부는 조회하지 않습니다. 쿨메신저에서 필요하면 확인하세요."
                    : "작업이 멈추거나 끝났습니다. 항목별 상태를 확인하세요."
            }
          />
          <ProgressBar header="실행 진행" value={run.total ? (run.progress * 100) / run.total : 0} />
          {run.issue && <InfoBar severity="error" message={issueLabel[run.issue]} />}
          <Caption>F8 또는 중지는 현재 호출 이후 반영될 수 있습니다. 이미 접수된 예약은 취소되지 않습니다.</Caption>
          <div className={s.row}>
            <Button
              disabled={!run.active && !pollFailure}
              onClick={() => {
                void cancel();
              }}
            >
              중지
            </Button>
            {pollFailure && <Button onClick={() => setPollFailure(false)}>상태 다시 확인</Button>}
          </div>
        </div>
      )}
      {view === "editor" && editor && (
        <Editor
          key={editor.id}
          deck={deck}
          draft={editor}
          recipients={recipients}
          busy={blocked}
          onSave={saveEditor}
          onCancel={() => {
            setEditor(null);
            setView("jobs");
          }}
        />
      )}
      {view === "batch" && (
        <WorkflowLayout
          title="여러 메시지 입력"
          input={{
            title: "예약표 또는 직접 입력",
            children: (
              <div className={s.stack}>
                <div className={s.row}>
                  <Button
                    disabled={blocked}
                    onClick={() => {
                      void readInput("batch");
                    }}
                  >
                    예약표 가져오기
                  </Button>
                  <Button
                    disabled={blocked || batch.length >= 500}
                    onClick={() => setBatch((old) => [...old, createDraft()])}
                  >
                    행 추가
                  </Button>
                </div>
                <SettingsExpander header="표 붙여넣기" defaultExpanded={!batch.length}>
                  <div className={s.stack}>
                    <TextBox
                      header="열 이름을 포함한 표"
                      multiline
                      rows={5}
                      value={paste}
                      onChange={setPaste}
                      disabled={blocked}
                      description="받는사람 · 예약날짜 · 예약시간 · 제목(선택) · 내용. 제목 열을 빼거나 순서를 바꾸면 열 이름도 복사하세요."
                    />
                    <Button
                      disabled={blocked}
                      onClick={() => {
                        void localTask(async () => {
                          setBatch(batchDrafts(await parseText(paste, new AbortController().signal)));
                          setBatchPage(0);
                          setError("");
                        });
                      }}
                    >
                      입력표로 옮기기
                    </Button>
                  </div>
                </SettingsExpander>
                {tables.length > 1 && (
                  <ComboBox
                    header="가져올 시트"
                    value={sheet}
                    options={tables.map((t) => ({ value: t.name, label: t.name }))}
                    disabled={blocked}
                    onChange={(name) => {
                      try {
                        setBatch(batchDrafts(tables.find((t) => t.name === name)?.values ?? []));
                        setSheet(name);
                        setBatchPage(0);
                      } catch {
                        setError("이 시트의 예약표 열 이름과 내용을 확인하세요.");
                      }
                    }}
                  />
                )}
              </div>
            ),
          }}
          options={{
            title: "표 입력",
            children: (
              <div className={s.stack}>
                {batch.slice(batchPage * 25, (batchPage + 1) * 25).map((d, offset) => {
                  const i = batchPage * 25 + offset;
                  const update = (change: Partial<Draft>) =>
                    setBatch((old) => old.map((row, n) => (n === i ? { ...row, ...change } : row)));
                  const resolved = resolveNames(d.recipientNames, d.bindings, recipients);
                  return (
                    <SettingsExpander
                      key={d.id}
                      header={`${i + 1}행 · ${d.title || d.body.slice(0, 60) || "메시지 입력"}`}
                      description={`${d.scheduledAt.replace("T", " ")} · ${d.recipientNames.join(" · ")} · 수정 필요 ${validateDraft(d).length + resolved.issues.length}개`}
                      defaultExpanded={batch.length === 1}
                    >
                      <div className={s.stack}>
                        <TextBox
                          header="받는 사람"
                          value={d.recipientNames.join(", ")}
                          disabled={blocked}
                          onChange={(value) => {
                            try {
                              update({ recipientNames: names(value) });
                            } catch {
                              update({ recipientNames: [value] });
                            }
                          }}
                        />
                        <RecipientMapping
                          requested={d.recipientNames}
                          bindings={d.bindings}
                          recipients={recipients}
                          onChange={(bindings) => update({ bindings })}
                          disabled={blocked}
                        />
                        <TextBox
                          header="예약일시 · YYYY-MM-DDTHH:MM"
                          value={d.scheduledAt}
                          disabled={blocked}
                          onChange={(scheduledAt) => update({ scheduledAt })}
                        />
                        <TextBox
                          header="제목 · 선택"
                          value={d.title}
                          disabled={blocked}
                          onChange={(title) => update({ title })}
                        />
                        <TextBox
                          header="내용"
                          value={d.body}
                          multiline
                          rows={4}
                          disabled={blocked}
                          onChange={(body) => update({ body })}
                        />
                        {d.missingAttachments.length > 0 && (
                          <InfoBar
                            severity="warning"
                            message="예약표 첨부는 경로를 자동으로 열지 않습니다. 개별 편집에서 파일을 선택하세요."
                          />
                        )}
                        <Button
                          disabled={blocked}
                          onClick={() => {
                            setEditor(d);
                            setView("editor");
                          }}
                        >
                          첨부 포함 개별 편집
                        </Button>
                        <Button
                          disabled={blocked}
                          onClick={() => setBatch((old) => old.filter((row) => row.id !== d.id))}
                        >
                          이 행 삭제
                        </Button>
                      </div>
                    </SettingsExpander>
                  );
                })}
                <div className={s.row}>
                  <Button disabled={!batchPage || blocked} onClick={() => setBatchPage((n) => n - 1)}>
                    이전 25행
                  </Button>
                  <Caption>
                    {batch.length}행 중 {batchPage * 25 + 1}행부터
                  </Caption>
                  <Button
                    disabled={(batchPage + 1) * 25 >= batch.length || blocked}
                    onClick={() => setBatchPage((n) => n + 1)}
                  >
                    다음 25행
                  </Button>
                </div>
              </div>
            ),
          }}
          preview={{
            title: "수신자 대조 · 전체 미리보기",
            children: (
              <Caption>
                {batch.length}건 중 수정 필요{" "}
                {
                  batch.filter(
                    (d) =>
                      validateDraft(d).length || resolveNames(d.recipientNames, d.bindings, recipients).issues.length,
                  ).length
                }
                건 · 모든 행이 확인되어야 함께 저장합니다.
              </Caption>
            ),
          }}
          run={{
            title: "초안만 저장",
            children: (
              <Button
                appearance="primary"
                disabled={blocked || !batch.length}
                onClick={() => {
                  void localTask(async () => {
                    const incoming = batch.map((d) => ({
                      ...d,
                      bindings: resolveNames(d.recipientNames, d.bindings, recipients).bindings,
                    }));
                    const errors = incoming.flatMap((d) => [
                      ...validateDraft(d),
                      ...resolveNames(d.recipientNames, d.bindings, recipients, true).issues,
                    ]);
                    if (errors.length) throw new Error(errors[0]);
                    await saveDrafts(incoming);
                    setBatch([]);
                  });
                }}
              >
                전체 {batch.length}건 초안 저장
              </Button>
            ),
          }}
          result={{
            title: "예약 메시지 목록",
            children: <Caption>초안 저장은 쿨메신저에 등록하지 않습니다. 목록에서 검토 후 한 번에 등록합니다.</Caption>,
          }}
        />
      )}
      {view === "workflow" && (
        <WorkflowLayout
          title="자료로 메시지 만들기"
          input={{
            title: "자료와 기간",
            children: (
              <div className={s.stack}>
                <ComboBox
                  header="메시지 양식"
                  value={recipe.id}
                  disabled={blocked}
                  options={state.recipes.map((r) => ({ value: r.id, label: r.name }))}
                  onChange={(id) => {
                    const found = state.recipes.find((r) => r.id === id);
                    if (found) {
                      setRecipe(structuredClone(found));
                      setReview(null);
                    }
                  }}
                />
                <Caption>
                  {recipe.preset === "lunch"
                    ? "선택 프리셋 · 확정 배정표에서 1·2차 지도 안내를 만듭니다. 배정을 계산하지 않습니다."
                    : "일반 자료 기본 · 한 행마다 한 메시지를 만듭니다."}
                </Caption>
                <div className={s.row}>
                  <Button
                    disabled={blocked}
                    onClick={() => {
                      void readInput("workflow");
                    }}
                  >
                    Excel·CSV 자료 읽기
                  </Button>
                  <Button
                    disabled={blocked}
                    onClick={() => {
                      void task(async () => {
                        await saveTemplate(deck, recipe.preset);
                      });
                    }}
                  >
                    이 양식의 빈 엑셀 받기
                  </Button>
                </div>
                <SettingsExpander header="자료 표 붙여넣기">
                  <div className={s.stack}>
                    <TextBox
                      header="열 이름을 포함한 자료"
                      value={paste}
                      onChange={setPaste}
                      multiline
                      rows={5}
                      disabled={blocked}
                    />
                    <Button
                      disabled={blocked}
                      onClick={() => {
                        void localTask(async () => {
                          const values = await parseText(paste, new AbortController().signal);
                          setTables([{ name: "붙여넣은 표", values }]);
                          setSheet("붙여넣은 표");
                          setSource("붙여넣은 표");
                          setRecipe((old) => ({ ...old, headerRows: detectHeader(values, old) }));
                          setReview(null);
                        });
                      }}
                    >
                      자료 읽기
                    </Button>
                  </div>
                </SettingsExpander>
                {tables.length > 0 && (
                  <ComboBox
                    header="자료 시트"
                    value={sheet}
                    options={tables.map((t) => ({ value: t.name, label: t.name }))}
                    disabled={blocked}
                    onChange={(name) => {
                      setSheet(name);
                      setReview(null);
                    }}
                  />
                )}
                <TextBox
                  header="자료 식별명 · 같은 자료를 다시 읽을 때 유지"
                  value={source}
                  onChange={(value) => {
                    setSource(value);
                    setReview(null);
                  }}
                  disabled={blocked}
                />
                <div className={s.row}>
                  <div className={s.field}>
                    <TextBox
                      header="시작일"
                      value={from}
                      onChange={(value) => {
                        setFrom(value);
                        setReview(null);
                      }}
                      disabled={blocked}
                    />
                  </div>
                  <div className={s.field}>
                    <TextBox
                      header="종료일"
                      value={to}
                      onChange={(value) => {
                        setTo(value);
                        setReview(null);
                      }}
                      disabled={blocked}
                    />
                  </div>
                </div>
                <Button disabled={blocked || !wfValues.length} onClick={() => setWfStep(2)}>
                  문구와 수신자 확인
                </Button>
              </div>
            ),
          }}
          options={{
            title: "문구와 수신자",
            children:
              wfStep < 2 ? (
                <Caption>자료를 읽고 기간을 선택하세요.</Caption>
              ) : (
                <div className={s.stack}>
                  <TextBox
                    header="제목 양식 · 선택"
                    value={recipe.title}
                    onChange={(title) => changeRecipe({ title })}
                    disabled={blocked}
                  />
                  <TextBox
                    header="내용 양식"
                    value={recipe.body}
                    onChange={(body) => changeRecipe({ body })}
                    multiline
                    rows={8}
                    disabled={blocked}
                  />
                  <Caption>
                    {recipe.preset === "lunch"
                      ? "{지도일} · {요일} · {담당목록} · {받는사람}"
                      : "{업무일} · {요일} · {받는사람} · {제목} · {내용} · {원본 열 이름}"}
                  </Caption>
                  <div className={s.row}>
                    <div className={s.field}>
                      <NumberBox
                        header="보내는 날 · 업무일 기준 차이"
                        value={recipe.offsetDays}
                        min={-90}
                        max={90}
                        step={1}
                        onChange={(offsetDays) => changeRecipe({ offsetDays: offsetDays ?? 0 })}
                        disabled={blocked}
                      />
                    </div>
                    <div className={s.field}>
                      <TextBox
                        header="발송 시각"
                        value={recipe.time}
                        onChange={(time) => changeRecipe({ time })}
                        disabled={blocked}
                      />
                    </div>
                  </div>
                  <SettingsExpander header="업무 양식 설정">
                    <div className={s.stack}>
                      <TextBox
                        header="업무 양식 이름"
                        value={recipe.name}
                        onChange={(name) => changeRecipe({ name })}
                        disabled={blocked}
                      />
                      <NumberBox
                        header="열 이름이 있는 행"
                        value={recipe.headerRows}
                        min={1}
                        max={10}
                        step={1}
                        disabled={blocked}
                        onChange={(headerRows) => changeRecipe({ headerRows: headerRows ?? 1 })}
                      />
                      {Object.entries(recipe.columns).map(([key, value]) => (
                        <ComboBox
                          key={key}
                          header={
                            (
                              {
                                date: "날짜",
                                recipient: "대상",
                                title: "제목",
                                body: "내용",
                                id: "업무 고유 ID",
                                exclude: "제외일",
                                first: "1차 담당자",
                                second: "2차 담당자",
                                first_only: "고사 1차만",
                              } as Record<string, string>
                            )[key] ?? key
                          }
                          value={value}
                          options={[
                            { value: "", label: "사용 안 함" },
                            ...(wfValues[recipe.headerRows - 1] ?? [])
                              .filter(Boolean)
                              .map((value) => ({ value, label: value })),
                          ]}
                          disabled={blocked}
                          onChange={(value) => changeRecipe({ columns: { ...recipe.columns, [key]: value } })}
                        />
                      ))}
                      {recipe.preset === "lunch" && (
                        <>
                          <ComboBox
                            header="받는 방식"
                            value={recipe.grouping}
                            options={[
                              { value: "date", label: "그날 담당자에게 전체 일정 함께 안내" },
                              { value: "row", label: "담당자마다 자신의 차시 안내" },
                            ]}
                            disabled={blocked}
                            onChange={(grouping) => changeRecipe({ grouping })}
                          />
                          {recipe.slots.map((slot, i) => (
                            <TextBox
                              key={slot.id}
                              header={`${slot.label} 지도 시간`}
                              value={slot.time}
                              disabled={blocked}
                              onChange={(time) =>
                                changeRecipe({ slots: recipe.slots.map((s, n) => (n === i ? { ...s, time } : s)) })
                              }
                            />
                          ))}
                        </>
                      )}
                    </div>
                  </SettingsExpander>
                  <div className={s.row}>
                    <Button
                      disabled={blocked}
                      onClick={() => {
                        void localTask(async () => {
                          const recipes = [...stateRef.current.recipes.filter((r) => r.id !== recipe.id), recipe];
                          if (recipes.length > 30) throw new Error("업무 양식은 30개까지 저장할 수 있습니다.");
                          await persist({ ...stateRef.current, recipes });
                          setNotice("업무 양식을 저장했습니다.");
                        });
                      }}
                    >
                      양식 저장
                    </Button>
                    <Button
                      disabled={blocked}
                      onClick={() =>
                        changeRecipe({
                          id: crypto.randomUUID(),
                          name: "새 일반 양식",
                          preset: "general",
                          columns: structuredClone(general.columns),
                          slots: [],
                          grouping: "row",
                        })
                      }
                    >
                      새 일반 양식 만들기
                    </Button>
                    <Button
                      appearance="primary"
                      disabled={blocked}
                      onClick={() => {
                        void workflowPreview();
                      }}
                    >
                      초안 검토
                    </Button>
                  </div>
                </div>
              ),
          }}
          preview={{
            title: "초안 확인",
            children: !review ? (
              <Caption>문구와 수신자를 검토하면 초안이 여기에 표시됩니다.</Caption>
            ) : (
              <div className={s.stack}>
                <Body>
                  새 초안 {review.candidates.filter((c) => c.change === "added").length} · 변경{" "}
                  {review.candidates.filter((c) => c.change === "changed").length} · 동일{" "}
                  {review.candidates.filter((c) => c.change === "unchanged").length} · 원본에서 사라짐{" "}
                  {review.deleted.length} · 제외일 {review.excluded}
                </Body>
                {review.issues.map((issue, i) => (
                  <InfoBar key={i} severity="error" message={issue} />
                ))}
                <RecipientMapping
                  requested={[...new Set(review.candidates.flatMap((c) => c.draft.recipientNames))]}
                  bindings={recipe.mapping}
                  recipients={recipients}
                  explicit
                  disabled={blocked}
                  onChange={(mapping) => changeRecipe({ mapping })}
                />
                <Button
                  disabled={blocked}
                  onClick={() => {
                    void workflowPreview();
                  }}
                >
                  수신자·설정 다시 검토
                </Button>
                {review.candidates.slice(previewPage * 25, (previewPage + 1) * 25).map((c) => (
                  <SettingsExpander
                    key={c.draft.id}
                    header={c.draft.title || c.draft.body.slice(0, 60) || "제목 없음"}
                    description={`${c.draft.scheduledAt.replace("T", " ")} · 원본 ${c.sourceRows.join(", ")}행 · ${c.change === "added" ? "새 초안" : c.change === "changed" ? "변경" : "동일"}`}
                    defaultExpanded={!!c.issues.length}
                  >
                    {messagePreview(c.draft)}
                    {c.issues.map((issue, i) => (
                      <InfoBar key={i} severity="error" message={issue} />
                    ))}
                  </SettingsExpander>
                ))}
                <div className={s.row}>
                  <Button disabled={!previewPage} onClick={() => setPreviewPage((n) => n - 1)}>
                    이전 25건
                  </Button>
                  <Button
                    disabled={(previewPage + 1) * 25 >= review.candidates.length}
                    onClick={() => setPreviewPage((n) => n + 1)}
                  >
                    다음 25건
                  </Button>
                </div>
              </div>
            ),
          }}
          run={{
            title: "초안만 저장",
            children: (
              <Button
                appearance="primary"
                disabled={
                  blocked ||
                  !review ||
                  reviewDirty ||
                  !!review.issues.length ||
                  review.candidates.some((c) => c.issues.length)
                }
                onClick={() => {
                  void localTask(async () => {
                    if (!review || reviewDirty) return;
                    const next = applyWorkflow(review, stateRef.current.drafts);
                    if (next.filter((d) => !d.archived).length > 500)
                      throw new Error("기본 목록 500건을 넘습니다. 지난 메시지를 보관하세요.");
                    await persist({ ...stateRef.current, drafts: next });
                    setView("jobs");
                    setSelected(review.candidates.filter((c) => !c.draft.retryLocked).map((c) => c.draft.id));
                    setNotice("자료 변경을 초안에 반영했습니다. 실제 예약은 아직 등록하지 않았습니다.");
                    setReview(null);
                  });
                }}
              >
                확인한 초안 저장
              </Button>
            ),
          }}
          result={{
            title: "예약 메시지 목록",
            children: (
              <Caption>
                원본·등록한 예약은 변경하지 않습니다. 원본 변경과 개별 수정이 겹치면 자동으로 반영하지 않습니다.
              </Caption>
            ),
          }}
        />
      )}
      {view === "history" && (
        <Button
          disabled={blocked}
          onClick={() => {
            void task(async () => {
              await saveFile(deck, "쿨예약실_업무기록.csv", historyCsv(stateRef.current.drafts));
            });
          }}
        >
          업무 기록 CSV 저장 · 수신자와 내용 포함
        </Button>
      )}
      {["jobs", "archives", "history"].includes(view) && (
        <div className={s.stack}>
          {view === "jobs" && navigation}
          <div className={s.row}>
            {view === "jobs" && (
              <ComboBox
                header="메시지 상태"
                value={filter}
                options={[
                  { value: "all", label: "전체" },
                  { value: "draft", label: "미등록" },
                  { value: "submitted", label: "등록 요청·불명" },
                ]}
                onChange={(value) => {
                  setFilter(value);
                  setPage(0);
                }}
              />
            )}
            <TextBox
              header="목록 검색"
              value={search}
              onChange={(value) => {
                setSearch(value);
                setPage(0);
              }}
            />
          </div>
          {view === "jobs" && (
            <div className={s.row}>
              <CheckBox
                content="보이는 미등록 메시지 전체 선택"
                checked={eligible.length > 0 && eligible.every((d) => selected.includes(d.id))}
                disabled={blocked || !eligible.length}
                onChange={(checked) => {
                  setSelected(checked ? eligible.map((d) => d.id) : []);
                  setPlan(null);
                }}
              />
              <Caption>{selected.length}건 선택</Caption>
              <Button
                disabled={blocked || !selected.length}
                onClick={() => {
                  void remove();
                }}
              >
                미등록 초안 삭제
              </Button>
              <Button
                disabled={blocked || !shown.length}
                onClick={() => {
                  void archive(
                    shown.filter((d) => d.scheduledAt.slice(0, 10) < kstNow().slice(0, 10)).map((d) => d.id),
                    true,
                  );
                }}
              >
                지난 메시지 정리
              </Button>
            </div>
          )}
          {view === "jobs" && (
            <div className={s.row}>
              <ComboBox
                header="실행 방식"
                value={mode}
                disabled={blocked}
                options={[
                  { value: "register", label: "예약 등록" },
                  { value: "simulate", label: "모의 실행 · 보내지 않음" },
                  { value: "inspect", label: "작성창만 대조 · 한 건" },
                  { value: "prepare", label: "자동 입력 · 보내지 않음 · 한 건" },
                ]}
                onChange={(value) => {
                  setMode(value);
                  setPlan(null);
                }}
              />
              <Button
                appearance="primary"
                disabled={blocked || !selected.length || !connection}
                onClick={() => {
                  void preview();
                }}
              >
                선택한 {selected.length}건 내용 검토
              </Button>
            </div>
          )}
          {!shown.length ? (
            <EmptyState
              title={view === "jobs" ? "아직 메시지가 없습니다" : "표시할 기록이 없습니다"}
              description="직접 작성하거나 예약표와 업무자료를 가져오세요."
            />
          ) : (
            <ListView
              header={
                view === "archives"
                  ? "보관한 메시지"
                  : view === "history"
                    ? "이 도구에서 확인한 실행 상태"
                    : "예약 메시지"
              }
              items={shown.slice(safePage * 50, (safePage + 1) * 50)}
              getKey={(d) => d.id}
              renderItem={(d) => (
                <div className={s.stack}>
                  <div className={s.row}>
                    {view === "jobs" && (
                      <CheckBox
                        content={d.title || d.body.split("\n").find(Boolean)?.slice(0, 80) || "제목 없음"}
                        checked={selected.includes(d.id)}
                        disabled={blocked || d.retryLocked}
                        onChange={(checked) => {
                          setSelected((old) => (checked ? [...old, d.id] : old.filter((id) => id !== d.id)));
                          setPlan(null);
                        }}
                      />
                    )}
                    <Caption>
                      {stateLabel[d.state]}
                      {d.retryLocked ? " · 자동 재등록 잠금" : ""}
                    </Caption>
                    <Caption>
                      {d.scheduledAt.replace("T", " ")} · {d.recipientNames.join(" · ")}
                    </Caption>
                    <Button
                      disabled={blocked || d.retryLocked}
                      onClick={() => {
                        setEditor(structuredClone(d));
                        setView("editor");
                        invalidate();
                      }}
                    >
                      편집
                    </Button>
                    <Button
                      disabled={blocked}
                      onClick={() => {
                        void archive([d.id], !d.archived);
                      }}
                    >
                      {d.archived ? "목록으로 복원" : "보관"}
                    </Button>
                  </div>
                  <SettingsExpander header="내용·수신자·첨부 보기">
                    {messagePreview(d)}
                    {d.missingAttachments.length > 0 && (
                      <InfoBar severity="warning" message="첨부파일을 다시 선택해야 합니다." />
                    )}
                  </SettingsExpander>
                </div>
              )}
            />
          )}
          <div className={s.row}>
            <Button disabled={!safePage} onClick={() => setPage((n) => n - 1)}>
              이전 50건
            </Button>
            <Caption>
              {safePage + 1}/{pageCount}쪽 · {shown.length}건
            </Caption>
            <Button disabled={safePage + 1 >= pageCount} onClick={() => setPage((n) => n + 1)}>
              다음 50건
            </Button>
          </div>
          {view === "history" && (
            <Caption>
              이 화면은 이 도구가 저장한 항목별 상태입니다. 쿨메신저의 도착 여부·전체 업무 기록을 조회하지 않습니다.
            </Caption>
          )}
          {plan && (
            <div className={s.stack}>
              <Body>
                등록 가능{" "}
                {
                  plan.preview.filter(
                    (p) => !p.retryLocked && !plan.issues.some((i) => !i.messageId || i.messageId === p.message.id),
                  ).length
                }
                건 · 수정 필요 {plan.issues.length}개
              </Body>
              {plan.issues.map((i, n) => (
                <InfoBar key={n} severity="error" message={issueLabel[i.code]} />
              ))}
              <Caption>
                이 PC의 쿨메신저에 로그인하고 조직도를 열어 두세요. 작성 중인 내용은 보관하고 다른 작성창은 닫으세요.
                입력·등록 중에는 마우스·키보드, 화면 잠금·절전을 멈춰 주세요.
              </Caption>
              {plan.preview.slice(previewPage * 25, (previewPage + 1) * 25).map((p) => (
                <SettingsExpander
                  key={p.message.id}
                  header={p.message.title || "제목 없음"}
                  description={`${p.message.scheduledAt.replace("T", " ")} · ${p.recipientLabels.join(" · ")}`}
                  defaultExpanded={plan.preview.length === 1}
                >
                  <div className={s.message}>
                    <Body>{p.message.body}</Body>
                  </div>
                  <Caption>{p.attachmentLabels.join(" · ")}</Caption>
                </SettingsExpander>
              ))}
              <div className={s.row}>
                <Button disabled={!previewPage} onClick={() => setPreviewPage((n) => n - 1)}>
                  이전 25건
                </Button>
                <Button
                  disabled={(previewPage + 1) * 25 >= plan.preview.length}
                  onClick={() => setPreviewPage((n) => n + 1)}
                >
                  다음 25건
                </Button>
                <Button
                  appearance="primary"
                  disabled={blocked || !!plan.issues.length || plan.preview.some((p) => p.retryLocked)}
                  onClick={() => {
                    void start();
                  }}
                >
                  {mode === "register" ? `${plan.preview.length}건 한 번에 등록 · 덱 확인창` : "실행 · 보내지 않음"}
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
