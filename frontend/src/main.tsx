import React, { lazy, Suspense, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import * as Collapsible from "@radix-ui/react-collapsible";
import * as Dialog from "@radix-ui/react-dialog";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Plus,
  PanelLeftClose,
  PanelLeft,
  ArrowDown,
  ChevronDown,
  Check,
  LoaderCircle,
  AlertCircle,
  MessageSquare,
  Map as MapIcon,
  Image,
  Layers,
  Compass,
  X,
  FileDown,
  Star,
  Focus,
  Square,
  RotateCcw,
  Satellite,
  FileJson,
} from "lucide-react";
import { client, eventStream } from "./api";
import { useWorkspace } from "./store";
import { Composer, composerBridge } from "./Composer";
const Canvas = lazy(() => import("./Canvas").then(module => ({ default: module.Canvas })));
import { DataEvidenceLink, DataEvidenceCard } from './DataEvidence';
import { ToolActivity } from './ToolActivity';
import { PythonArtifacts } from './PythonArtifacts';
import type { Capabilities, Detail, Message, Observation, Run } from "./types";
import "./style.css";
const qc = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});
const key = (id?: string) => ["conversation", id];
const names: Record<string, string> = {
  queued: "等待执行",
  accepted: "已接收",
  pending: "待采纳",
  adopted: "已采纳",
  running: "正在分析",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已停止",
  cancelling: "正在停止",
  budget_exhausted: "等待追加预算",
  external_service_unavailable: "服务暂不可用",
};
function invalidate(id?: string) {
  if (id) qc.invalidateQueries({ queryKey: key(id) });
  qc.invalidateQueries({ queryKey: ["conversations"] });
}
function RuntimeStatus({ capabilities }: { capabilities?: Capabilities }) {
  const execution = capabilities?.execution || capabilities?.sandbox;
  if (!execution?.available) return null;
  return (
    <span className="runtime-status" title={execution.runtime}>
      <span className="runtime-dot" />
      本地执行
    </span>
  );
}
const isMac = navigator.platform.includes("Mac");
const exampleQuestions = [
  "港区的仓储设施主要分布在哪里？",
  "这两期影像里，哪些地方变化最明显？",
];
function fileSize(bytes?: number) {
  if (!bytes && bytes !== 0) return "";
  return bytes < 1024 * 1024
    ? `${Math.ceil(bytes / 1024)} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function messageTime(iso: string) {
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  const sameDay = d.toDateString() === new Date().toDateString();
  return {
    text: sameDay ? hm : `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`,
    title: d.toLocaleString("zh-CN"),
  };
}
const pendingRefresh = new Map<string, number>();
function received(id: string, event: any) {
  const p = event.payload || {};
  qc.setQueryData<Detail>(key(id), (d) => {
    if (!d) return d;
    const next = {
      ...d,
      conversation: { ...d.conversation, event_sequence: event.sequence },
    };
    const replace = <T extends { id: string | number }>(
      rows: T[],
      value: T,
    ) => [...rows.filter((x) => x.id !== value.id), value];
    if (p.message)
      next.messages = replace(d.messages, p.message as Message).sort(
        (a, b) => a.sequence - b.sequence,
      );
    if (p.attachment) next.attachments = replace(d.attachments, p.attachment);
    if (p.observation)
      next.observations = replace(d.observations, p.observation);
    if (p.run) {
      next.runs = replace(d.runs, p.run as Run).sort((a, b) => b.id - a.id);
      next.conversation.active_run = ["completed", "cancelled"].includes(
        p.run.status,
      )
        ? null
        : p.run;
    }
    if (p.conversation) next.conversation = p.conversation;
    return next;
  });
  if (event.type === "tool.completed") {
    if (!pendingRefresh.has(id))
      pendingRefresh.set(
        id,
        window.setTimeout(() => {
          pendingRefresh.delete(id);
          qc.invalidateQueries({ queryKey: key(id) });
          qc.invalidateQueries({ queryKey: ["run"] });
        }, 1500),
      );
  }
  if (event.type === "message.created")
    qc.invalidateQueries({ queryKey: ["conversations"] });
}
function Logo() {
  return (
    <span className="logo">
      <span className="logo-mark">
        <img src="/static/v3-mark.svg" width="20" height="20" alt="" />
      </span>
      Satellite<span>Sense</span>
    </span>
  );
}
function Navigation() {
  const s = useWorkspace(),
    { data, error } = useQuery({
      queryKey: ["conversations"],
      queryFn: client.conversations,
    });
  return (
    <aside className={"navigation " + (!s.nav ? "collapsed" : "")}>
      <div className="nav-top">
        <Logo />
        <button aria-label="收起会话导航" onClick={() => s.set({ nav: false })}>
          <PanelLeftClose size={17} />
        </button>
      </div>
      <button
        className="new-conversation"
        onClick={() => s.selectConversation()}
      >
        <Plus size={16} /> 新的影像问答<span>{isMac ? "⌘ N" : "Ctrl N"}</span>
      </button>
      <div className="nav-section">工作空间</div>
      <button
        className="nav-destination active"
        onClick={() => s.set({ mobile: "chat" })}
      >
        <MessageSquare size={15} /> 影像问答
      </button>
      <div className="nav-section recent-heading">
        最近会话 <span className="nav-count">{data?.items.length || 0}</span>
      </div>
      <div className="conversation-list">
        {error && <div className="muted">暂时无法加载会话</div>}
        {data?.items.map((c) => (
          <button
            key={c.id}
            className={
              "conversation-link " +
              (s.conversationId === c.id ? "current" : "")
            }
            onClick={() => {
              s.selectConversation(c.id);
              if (innerWidth < 800) s.set({ nav: false });
            }}
          >
            <MessageSquare size={13} />
            <span>{c.title}</span>
            {c.starred ? (
              <Star size={12} />
            ) : c.active_run &&
              ["queued", "running"].includes(c.active_run.status) ? (
              <i className="activity-dot" />
            ) : null}
          </button>
        ))}
        {data?.items.length === 0 && (
          <div className="nav-empty">
            <Satellite size={56} strokeWidth={1} />
            <p>
              发起一次提问，
              <br />
              会话会自动保存在这里。
            </p>
          </div>
        )}
      </div>
      <div className="nav-footer">
        <span className="workspace-avatar">S</span>
        <div>
          个人工作区<small>大场景遥感图像智能问答</small>
        </div>
        <a className="legacy-link" href="/legacy/">
          经典版
        </a>
      </div>
    </aside>
  );
}
function Evidence({ o }: { o: Observation }) {
  const s = useWorkspace();
  return (
    <button
      className="evidence-link"
      onClick={() =>
        s.set({
          selectedObservation: o.id,
          selectedAttachment: o.attachment_id,
        canvasMode: o.kind === 'change_candidate' ? 'compare' : 'image',
        ...(o.kind === 'change_candidate' ? {secondAttachment:o.metadata?.comparison_attachment_id} : {}),
          mobile: "canvas",
        })
      }
    >
      <span className="evidence-index">
        <MapIcon size={12} />
      </span>
      {o.label}
      <span>↗</span>
    </button>
  );
}
function MessageRow({
  message: m,
  detail,
}: {
  message: Message;
  detail: Detail;
}) {
  const s = useWorkspace();
  const ids =
    m.attachment_ids ||
    m.parts.filter((p) => p.type === "image_ref").map((p) => p.attachment_id);
  const refs = m.parts
    .filter((p) => p.type === "observation_ref")
    .map((p) => p.id);
  const observations = detail.observations
    .filter((o) => refs.includes(o.id))
    .sort(
      (a, b) => Number(b.kind === "finding") - Number(a.kind === "finding"),
    );
  return (
    <article className={"message " + m.role}>
      <div className="message-author">
        {m.role === "user" ? (
          <span className="user-mark">你</span>
        ) : (
          <Compass size={17} strokeWidth={1.5} />
        )}
        {m.role !== "user" && <span>SatelliteSense</span>}
        <time title={messageTime(m.created_at).title}>
          {messageTime(m.created_at).text}
        </time>
      </div>
      {ids.length > 0 && (
        <div className="message-images">
          {ids.map((id: string) => {
            const a = detail.attachments.find((a) => a.id === id);
            return (
              a && (
                <button
                  key={id}
                  onClick={() =>
                    s.set({
                      selectedAttachment: id,
                      canvasMode: "image",
                      mobile: "canvas",
                    })
                  }
                >
                  {a.preview_url ? (
                    <img src={a.preview_url} alt={a.name} />
                  ) : (
                    <Image size={28} />
                  )}
                  <span>
                    {a.name}
                    <small>
                      {a.width
                        ? a.width.toLocaleString() +
                          " × " +
                          a.height.toLocaleString()
                        : names[a.status] || "处理中"}
                    </small>
                  </span>
                </button>
              )
            );
          })}
        </div>
      )}
      {m.content && (
        <div className="message-content">
          {m.role === "assistant" ? (
            <Markdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({ href, children }) => (
                  <a href={href} target="_blank" rel="noreferrer">
                    {children}
                  </a>
                ),
              }}
            >
              {m.content}
            </Markdown>
          ) : (
            <p>{m.content}</p>
          )}
        </div>
      )}
      <PythonArtifacts message={m} artifacts={detail.artifacts} />
      {observations.length > 0 && (
        <div className="evidence-links">
          {observations.slice(0, 6).map((o) => (
            <Evidence key={o.id} o={o} />
          ))}
          {observations.length > 6 && (
            <details className="more-evidence">
              <summary>另外 {observations.length - 6} 处观察</summary>
              {observations.slice(6).map((o) => (
                <Evidence key={o.id} o={o} />
              ))}
            </details>
          )}
        </div>
      )}
      {m.parts
        .filter(p=>p.type==='evidence_ref')
        .map(p=>detail.evidence?.find(e=>e.id===p.id))
        .filter(e=>!!e)
        .map(e=><DataEvidenceLink key={e.id} evidence={e}/>)}
      {m.parts
        .filter((p) => p.type === "limitations")
        .map((p, i) => (
          <ul key={i} className="limitations-list">
            {p.items.map((item: string, j: number) => (
              <li key={j}>{item}</li>
            ))}
          </ul>
        ))}
      {m.role === "user" &&
        ["queued", "pending", "accepted"].includes(m.status) && (
          <small className="message-state">
            {names[m.status]}
            {m.delivery === "queue" ? " · 完成后处理" : ""}
          </small>
        )}
    </article>
  );
}
function RunProcess({
  run,
  conversationId,
}: {
  run: Run;
  conversationId: string;
}) {
  const [error, setError] = useState("");
  const running = ["queued", "running", "planning"].includes(run.status);
  async function action(name: string, budget?: object) {
    try {
      await client.action(run.id, name, budget);
      invalidate(conversationId);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const actionText = run.current_action || names[run.status];
  return (
    <Collapsible.Root
      className={
        "run-process " + (run.status === "completed" ? "finished" : "")
      }
    >
      <div className="run-line">
        <Collapsible.Trigger className="run-trigger">
          {running ? (
            <LoaderCircle size={14} className="spin" />
          ) : run.status === "completed" ? (
            <Check size={14} />
          ) : (
            <AlertCircle size={14} />
          )}
          <span title={actionText}>
            {actionText.length > 24 ? actionText.slice(0, 24) + "…" : actionText}
          </span>
          <ChevronDown size={13} />
        </Collapsible.Trigger>
        {running && (
          <button
            className="line-stop"
            title="停止运行"
            aria-label="停止运行"
            onClick={() => action("stop")}
          >
            <Square size={10} fill="currentColor" strokeWidth={0} />
          </button>
        )}
      </div>
      {running && run.public_update && (
        <p className="public-update" aria-live="polite">
          {run.public_update}
        </p>
      )}
      <Collapsible.Content className="run-details">
        <ToolActivity runId={run.id}/>
        {run.error && <p className="error">{run.error}</p>}
        {run.plan?.map((step, i) => (
          <div className="plan-item" key={i}>
            {step.status === "completed" ? (
              <Check size={12} />
            ) : (
              <span className="step-dot" />
            )}
            {step.label}
          </div>
        ))}
        <div className="usage-row">
          <span>{run.model}</span>
          <span>{run.usage?.controller_calls || 0} 次主控调用</span>
          <span>{Math.round(run.usage?.wall_seconds || 0)}s</span>
        </div>
        {["failed", "external_service_unavailable", "cancelled"].includes(
          run.status,
        ) && (
          <button className="outline-button" onClick={() => action("resume")}>
            <RotateCcw size={13} /> 从检查点继续
          </button>
        )}
        {run.status === "budget_exhausted" && (
          <button
            className="outline-button"
            onClick={() =>
              action("extend_budget", {
                controller_calls: 32,
                vision_calls: 32,
                wall_seconds: 1800,
              })
            }
          >
            追加 30 分钟与 32 次调用
          </button>
        )}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}
function Chat({ detail }: { detail: Detail }) {
  const parent = useRef<HTMLDivElement>(null),
    bottom = useRef(true),
    lastCount = useRef(0);
  const [newProgress, setNewProgress] = useState(false),
    [connected, setConnected] = useState(true);
  const rows = detail.messages;
  const virtual = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parent.current,
    estimateSize: () => 320,
    getItemKey: (i) => rows[i].id,
    overscan: 5,
  });
  useEffect(
    () =>
      eventStream(
        detail.conversation.id,
        detail.conversation.event_sequence,
        (e) => received(detail.conversation.id, e),
        setConnected,
      ),
    [detail.conversation.id],
  );
  useEffect(() => {
    if (rows.length !== lastCount.current) {
      if (bottom.current)
        requestAnimationFrame(() =>
          virtual.scrollToIndex(rows.length - 1, { align: "end" }),
        );
      else setNewProgress(true);
      lastCount.current = rows.length;
    }
  }, [rows.length]);
  const run = detail.conversation.active_run || detail.runs[0];
  return (
    <section className="chat-panel">
      <header className="chat-heading">
        <span>{detail.conversation.title}</span>
        <small>{connected ? "会话已保存" : "正在重新连接…"}</small>
      </header>
      <div
        ref={parent}
        className="messages-scroll"
        onScroll={() => {
          const e = parent.current!;
          bottom.current = e.scrollHeight - e.scrollTop - e.clientHeight < 100;
          if (bottom.current) setNewProgress(false);
        }}
      >
        {rows.length === 0 ? (
          <div className="chat-empty">
            <Compass size={23} />
            <h2>想了解这片区域的什么？</h2>
            <p>
              在地图上框选，或添加影像。
              <br />
              从一个问题开始，随时继续追问。
            </p>
          </div>
        ) : (
          <div style={{ height: virtual.getTotalSize(), position: "relative" }}>
            {virtual.getVirtualItems().map((item) => (
              <div
                key={item.key}
                data-index={item.index}
                ref={virtual.measureElement}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: "translateY(" + item.start + "px)",
                }}
              >
                <MessageRow message={rows[item.index]} detail={detail} />
              </div>
            ))}
          </div>
        )}
      </div>
      {newProgress && (
        <button
          className="new-progress"
          onClick={() => {
            virtual.scrollToIndex(rows.length - 1, {
              align: "end",
              behavior: "smooth",
            });
            bottom.current = true;
            setNewProgress(false);
          }}
        >
          <ArrowDown size={14} /> 新的进展
        </button>
      )}
      {run && <RunProcess run={run} conversationId={detail.conversation.id} />}
      <Composer detail={detail} onChange={invalidate} />
    </section>
  );
}
function Welcome() {
  const s = useWorkspace();
  return (
    <main className="welcome">
      <img
        className="welcome-mark"
        src="/static/v3-mark.svg"
        width="40"
        height="40"
        alt=""
      />
      <div className="welcome-eyebrow">
        <span className="gold-line" /> 看见全局，理解每一处细节
      </div>
      <h1>
        向这片世界
        <br />
        提出你的问题。
      </h1>
      <p>大场景遥感图像智能问答</p>
      <Composer large onChange={invalidate} />
      <div className="welcome-actions">
        <button
          onClick={async () => {
            const r = await client.create();
            s.selectConversation(r.conversation.id);
            s.set({ canvasMode: "map" });
            invalidate(r.conversation.id);
          }}
        >
          <MapIcon size={17} />
          <div>
            在地图上开始<span>选择你关心的城市、河流或港区</span>
          </div>
          <span>↗</span>
        </button>
        <button
          className="welcome-note"
          title="选择要上传的影像"
          onClick={() => composerBridge.pick?.()}
        >
          <Image size={18} />
          <span>
            上传 PNG、JPEG 或 GeoTIFF
            <br />
            <small>保留原始细节，支持多图与局部追问</small>
          </span>
        </button>
      </div>
      <div className="welcome-examples">
        <span>可以这样问</span>
        {exampleQuestions.map((q) => (
          <button
            key={q}
            className="welcome-example"
            onClick={() => composerBridge.fill?.(q)}
          >
            “{q}”
          </button>
        ))}
      </div>
    </main>
  );
}
function Drawer({ detail }: { detail: Detail }) {
  const s = useWorkspace();
  const groups = [
    detail.attachments.length,
    detail.observations.length,
    detail.evidence?.length || 0,
    detail.artifacts.length,
  ];
  const empty = groups.every((n) => n === 0);
  return (
    <Dialog.Root open={s.drawer} onOpenChange={(drawer) => s.set({ drawer })}>
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay" />
        <Dialog.Content className="evidence-drawer">
          <div className="drawer-heading">
            <div>
              <Dialog.Title>影像与证据</Dialog.Title>
              <Dialog.Description>
                当前会话的空间观察和可下载成果
              </Dialog.Description>
            </div>
            <Dialog.Close aria-label="关闭证据面板">
              <X size={18} />
            </Dialog.Close>
          </div>
          {empty ? (
            <div className="drawer-empty">
              <Compass size={20} strokeWidth={1.5} />
              <p>发起一次分析后，证据将汇总在这里</p>
            </div>
          ) : (
            <>
              {detail.attachments.length > 0 && (
                <>
                  <h3>影像 · {detail.attachments.length}</h3>
                  {detail.attachments.map((a) => (
                    <button
                      className="drawer-asset"
                      key={a.id}
                      onClick={() =>
                        s.set({
                          selectedAttachment: a.id,
                          canvasMode: "image",
                          drawer: false,
                          mobile: "canvas",
                        })
                      }
                    >
                      {a.preview_url ? (
                        <img alt="" src={a.preview_url} />
                      ) : (
                        <Image size={20} />
                      )}
                      <span>
                        {a.name}
                        <small>
                          {a.width.toLocaleString()} ×{" "}
                          {a.height.toLocaleString()} · {a.crs || "图像坐标"}
                        </small>
                      </span>
                    </button>
                  ))}
                </>
              )}
              {detail.observations.length > 0 && (
                <>
                  <h3>空间观察 · {detail.observations.length}</h3>
                  {detail.observations.map((o) => (
                    <Evidence key={o.id} o={o} />
                  ))}
                </>
              )}
              {(detail.evidence?.length || 0) > 0 && (
                <>
                  <h3>数据依据 · {detail.evidence?.length || 0}</h3>
                  {(detail.evidence || [])
                    .slice()
                    .sort(
                      (a, b) =>
                        Number(b.id === s.selectedEvidence) -
                        Number(a.id === s.selectedEvidence),
                    )
                    .map((e) => (
                      <DataEvidenceCard key={e.id} evidence={e} />
                    ))}
                </>
              )}
              {detail.artifacts.length > 0 && (
                <>
                  <h3>成果文件 · {detail.artifacts.length}</h3>
                  {detail.artifacts.map((a) => (
                    <a className="artifact-download" key={a.id} href={a.download_url}>
                      {a.mime_type?.startsWith("image/") ? (
                        <Image size={16} />
                      ) : a.mime_type?.includes("json") ? (
                        <FileJson size={16} />
                      ) : (
                        <FileDown size={16} />
                      )}
                      {a.title}
                      <small>{fileSize(a.metadata?.size_bytes)}</small>
                    </a>
                  ))}
                </>
              )}
            </>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
function Workspace() {
  const s = useWorkspace(),
    [chatWidth, setChatWidth] = useState(440),
    [focus, setFocus] = useState(false);
  const { data, error, isLoading } = useQuery({
    queryKey: key(s.conversationId),
    queryFn: () => client.detail(s.conversationId!),
    enabled: !!s.conversationId,
    refetchInterval: (q) =>
      q.state.data?.attachments.some((a) =>
        ["pending", "processing", "uploading"].includes(a.status),
      )
        ? 2000
        : false,
  });
  const { data: capabilities } = useQuery({
    queryKey: ["capabilities"],
    queryFn: client.capabilities,
    staleTime: 60_000,
  });
  useEffect(() => {
    const keydown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "n") {
        e.preventDefault();
        s.selectConversation();
      }
      if (e.key === "Escape") s.set({ drawer: false });
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, []);
  function resize(e: React.PointerEvent) {
    e.preventDefault();
    const x = e.clientX,
      w = chatWidth;
    const move = (p: PointerEvent) =>
      setChatWidth(Math.max(360, Math.min(720, w + p.clientX - x)));
    const end = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
  }
  return (
    <div
      className={
        "app " +
        (s.nav ? "nav-open " : "") +
        (focus ? "focus-chat " : "") +
        (s.mobile === "canvas" ? "mobile-canvas" : "")
      }
      style={{ "--chat-width": chatWidth + "px" } as React.CSSProperties}
    >
      <Navigation />
      <div className="workspace">
        <header className="workspace-bar">
          <div>
            {!s.nav && (
              <button
                aria-label="展开会话导航"
                onClick={() => s.set({ nav: true })}
              >
                <PanelLeft size={18} />
              </button>
            )}
            <span className="workspace-breadcrumb">
              工作空间 <span>/</span> <strong>影像问答</strong>
            </span>
          </div>
          <div>
            {data && (
              <>
                <button
                  aria-label="收藏当前会话"
                  className={data.conversation.starred ? "selected" : ""}
                  onClick={() =>
                    client
                      .update(data.conversation.id, {
                        starred: !data.conversation.starred,
                      })
                      .then(() => invalidate(data.conversation.id))
                  }
                >
                  <Star size={15} />
                </button>
                <button
                  aria-label={focus ? "显示画布" : "专注对话"}
                  onClick={() => setFocus(!focus)}
                >
                  <Focus size={17} />
                </button>
                <button
                  aria-label="证据与图层"
                  onClick={() => s.set({ drawer: true })}
                >
                  <Layers size={17} />
                </button>
              </>
            )}
            <RuntimeStatus capabilities={capabilities} />
          </div>
        </header>
        {!s.conversationId ? (
          <Welcome />
        ) : error && !data ? (
          <main className="page-error">
            <AlertCircle size={25} />
            <h2>暂时无法打开会话</h2>
            <p>{(error as Error).message}</p>
            <button
              className="outline-button"
              onClick={() => invalidate(s.conversationId)}
            >
              重试
            </button>
            <button onClick={() => s.selectConversation()}>返回工作空间</button>
          </main>
        ) : isLoading || !data ? (
          <div className="page-skeleton" aria-busy="true" aria-label="正在打开工作空间">
            <div className="msg-skeleton" style={{ height: 14, width: "60%" }} />
            <div className="msg-skeleton" style={{ height: 14, width: "80%" }} />
            <div className="msg-skeleton" style={{ height: 14, width: "45%" }} />
          </div>
        ) : (
          <>
            <div className="workbench">
              <Chat key={data.conversation.id} detail={data} />
              <div
                className="resizer"
                role="separator"
                tabIndex={0}
                aria-label="调整聊天宽度"
                aria-orientation="vertical"
                aria-valuenow={chatWidth}
                aria-valuemin={360}
                aria-valuemax={720}
                aria-valuetext={`聊天宽度 ${chatWidth} 像素`}
                onPointerDown={resize}
                onDoubleClick={() => setChatWidth(440)}
                onKeyDown={(e) => {
                  if (e.key === "ArrowLeft")
                    setChatWidth((w) => Math.max(360, w - 20));
                  if (e.key === "ArrowRight")
                    setChatWidth((w) => Math.min(720, w + 20));
                }}
              />
              <Suspense fallback={<section className="canvas-panel" aria-busy="true" aria-label="正在载入影像画布" />}><Canvas
                detail={data}
                onChange={() => invalidate(data.conversation.id)}
              /></Suspense>
            </div>
            <Drawer detail={data} />
            <nav className="mobile-tabs">
              <button
                className={s.mobile === "chat" ? "selected" : ""}
                onClick={() => s.set({ mobile: "chat" })}
              >
                <MessageSquare size={16} />
                对话
              </button>
              <button
                className={s.mobile === "canvas" ? "selected" : ""}
                onClick={() => s.set({ mobile: "canvas" })}
              >
                <MapIcon size={16} />
                画布
              </button>
            </nav>
          </>
        )}
      </div>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}>
      <Workspace />
    </QueryClientProvider>
  </React.StrictMode>,
);
