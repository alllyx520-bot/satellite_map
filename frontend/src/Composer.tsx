import { useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  Paperclip,
  Square,
  X,
  ImagePlus,
  LoaderCircle,
} from "lucide-react";
import { client } from "./api";
import { useWorkspace } from "./store";
import type { Detail, Attachment } from "./types";
type Props = {
  detail?: Detail;
  onChange: (id?: string) => void;
  large?: boolean;
};
export const composerBridge: {
  fill?: (text: string) => void;
  pick?: () => void;
} = {};
export function Composer({ detail, onChange, large = false }: Props) {
  const s = useWorkspace(),
    input = useRef<HTMLInputElement>(null),
    textRef = useRef<HTMLTextAreaElement>(null);
  const [text, setText] = useState(""),
    [delivery, setDelivery] = useState("steer"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [progress, setProgress] = useState<{ name: string; value: number } | null>(
      null,
    );
  const requestId = useRef(crypto.randomUUID()),
    sending = useRef(false),
    dragDepth = useRef(0),
    conversation = detail?.conversation.id;
  const [dragging, setDragging] = useState(false);
  useEffect(() => {
    setText("");
    setError("");
    requestId.current = crypto.randomUUID();
  }, [conversation]);
  useEffect(() => {
    composerBridge.fill = (value: string) => {
      setText(value);
      textRef.current?.focus();
    };
    composerBridge.pick = () => input.current?.click();
    return () => {
      composerBridge.fill = undefined;
      composerBridge.pick = undefined;
    };
  }, []);
  useEffect(() => {
    const el = textRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 220) + "px";
    }
  }, [text]);
  async function files(files: File[]) {
    setError("");
    try {
      for (const file of files) {
        if (!/\.(png|jpe?g|tiff?)$/i.test(file.name))
          throw new Error("支持 PNG、JPEG 和 GeoTIFF 影像");
        setProgress({ name: file.name, value: 0 });
        const r = await client.upload(file, conversation, (value) =>
          setProgress({ name: file.name, value }),
        );
        s.addDraft(r.attachment);
        onChange(conversation);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setProgress(null);
    }
  }
  async function send() {
    if (
      sending.current ||
      (!text.trim() && !s.drafts.length && !s.references.length)
    )
      return;
    sending.current = true;
    setBusy(true);
    setError("");
    try {
      const id =
        conversation ||
        (await client.create(text.slice(0, 70) || "影像问答")).conversation.id;
      await client.send(id, {
        content: text,
        attachment_ids: s.drafts.map((a) => a.id),
        attachment_names: Object.fromEntries(
          s.drafts.map((a) => [a.id, a.name.trim()]),
        ),
        references: s.references.map((id) => ({ type: "observation_ref", id })),
        delivery,
        request_id: requestId.current,
      });
      setText("");
      s.set({
        drafts: [],
        references: [],
        conversationId: id,
        ...(s.drafts.length
          ? { selectedAttachment: s.drafts[0].id, canvasMode: "image" as const }
          : {}),
      });
      window.sessionStorage.setItem("satellitesense:conversation", id);
      requestId.current = crypto.randomUUID();
      onChange(id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }
  const active = detail?.conversation.active_run;
  const running =
    active &&
    ["queued", "running", "planning", "cancelling"].includes(active.status);
  const draft = (a: Attachment) =>
    detail?.attachments.find((x) => x.id === a.id) || a;
  return (
    <div
      className={"composer-wrap " + (large ? "large" : "")}
      onDragOver={(e) => e.preventDefault()}
      onDragEnter={(e) => {
        e.preventDefault();
        dragDepth.current += 1;
        setDragging(true);
      }}
      onDragLeave={() => {
        dragDepth.current -= 1;
        if (dragDepth.current <= 0) {
          dragDepth.current = 0;
          setDragging(false);
        }
      }}
      onDrop={(e) => {
        e.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        files(Array.from(e.dataTransfer.files));
      }}
    >
      <div
        className={"composer" + (dragging ? " dragging" : "")}
        onPaste={(e) => {
          if (e.clipboardData.files.length) {
            e.preventDefault();
            files(Array.from(e.clipboardData.files));
          }
        }}
      >
        {(s.drafts.length > 0 || s.references.length > 0) && (
          <div className="draft-list">
            {s.drafts.map((original) => {
              const a = draft(original);
              return (
                <div className="attachment-chip" key={a.id}>
                  {a.preview_url ? (
                    <img src={a.preview_url} alt="" />
                  ) : (
                    <ImagePlus size={22} />
                  )}
                  <div>
                    <input
                      aria-label="附件名称"
                      value={original.name}
                      onChange={(e) =>
                        s.set({
                          drafts: s.drafts.map((x) =>
                            x.id === a.id ? { ...x, name: e.target.value } : x,
                          ),
                        })
                      }
                    />
                    <small>
                      {a.status === "ready"
                        ? `${a.width.toLocaleString()} × ${a.height.toLocaleString()}`
                        : a.status === "failed"
                          ? a.error
                          : "正在准备影像"}
                    </small>
                  </div>
                  <button
                    aria-label={"移除" + a.name}
                    onClick={() => s.removeDraft(a.id)}
                  >
                    <X size={14} />
                  </button>
                </div>
              );
            })}
            {s.references.map((id) => (
              <div className="reference-chip" key={id}>
                {detail?.observations.find((o) => o.id === id)?.label ||
                  "位置引用"}
                <button
                  aria-label="移除位置引用"
                  onClick={() =>
                    s.set({ references: s.references.filter((x) => x !== id) })
                  }
                >
                  <X size={12} />
                </button>
              </div>
            ))}
          </div>
        )}
        <textarea
          ref={textRef}
          value={text}
          rows={large ? 3 : 2}
          aria-label="遥感影像问题"
          placeholder={
            large
              ? "这里有什么？哪里发生了变化？\n描述你的问题，或把影像拖到这里。"
              : "继续提问，或添加影像与位置…"
          }
          onChange={(e) => {
            setText(e.target.value);
            requestId.current = crypto.randomUUID();
          }}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault();
              send();
            }
          }}
        />
        {progress && (
          <div className="upload-progress" role="status">
            <LoaderCircle size={13} className="spin" />
            <span>{progress.name}</span>
            <span>{Math.round(progress.value * 100)}%</span>
            <progress value={progress.value} max={1} />
          </div>
        )}
        <div className="composebar">
          <input
            hidden
            ref={input}
            type="file"
            multiple
            accept=".png,.jpg,.jpeg,.tif,.tiff"
            onChange={(e) => {
              files(Array.from(e.target.files || []));
              e.target.value = "";
            }}
          />
          <button
            aria-label="添加影像"
            title="上传或粘贴影像，最大 1 GB"
            disabled={!!progress}
            onClick={() => input.current?.click()}
          >
            <Paperclip size={17} />
            <span>影像</span>
          </button>
          <span className="model-label">DeepSeek Flash</span>
          {running && (
            <div
              className="steer-switch"
              role="radiogroup"
              aria-label="运行中消息处理方式"
            >
              {(
                [
                  ["steer", "补充当前任务"],
                  ["queue", "完成后处理"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  role="radio"
                  aria-checked={delivery === value}
                  className={delivery === value ? "selected" : ""}
                  onClick={() => setDelivery(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
          <div className="compose-spacer" />
          {running && (
            <button
              className="composer-stop"
              aria-label="停止任务"
              title="停止任务"
              onClick={() =>
                client
                  .action(active.id, "stop")
                  .then(() => onChange(conversation))
                  .catch((e) => setError(e.message))
              }
            >
              <Square size={10} fill="currentColor" strokeWidth={0} />
            </button>
          )}
          <button
            className="send"
            aria-label="发送问题"
            onClick={send}
            disabled={
              busy ||
              !!progress ||
              (!text.trim() && !s.drafts.length && !s.references.length)
            }
          >
            {busy ? (
              <LoaderCircle size={18} className="spin" />
            ) : (
              <ArrowUp size={17} />
            )}
          </button>
        </div>
        {dragging && <div className="composer-drop-hint">松开以上传影像</div>}
      </div>
      <div className="composer-foot">
        {!large && (
          <span>
            {running
              ? "新要求会在安全执行边界采纳"
              : "支持大图 · GeoTIFF · 多区域问答"}
          </span>
        )}
        <span>
          Enter 发送
          {text.length > 800 && (
            <span
              className={"char-count" + (text.length > 2000 ? " over" : "")}
            >
              已输入 {text.length} 字
            </span>
          )}
        </span>
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
