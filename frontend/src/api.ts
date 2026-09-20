import type { Attachment, Capabilities, Conversation, Detail, Message, Run } from "./types";
const api = "/api/v3";
const csrf = () =>
  decodeURIComponent(
    document.cookie
      .split("; ")
      .find((x) => x.startsWith("csrftoken="))
      ?.split("=")[1] || "",
  );
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (init.method && init.method !== "GET") headers.set("X-CSRFToken", csrf());
  const response = await fetch(api + path, {
    ...init,
    credentials: "same-origin",
    headers,
  });
  const value = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(
      value?.error?.message || "请求失败 (" + response.status + ")",
    );
  return value;
}
export const client = {
  conversations: () => request<{ items: Conversation[] }>("/conversations"),
  create: (title?: string) =>
    request<{ conversation: Conversation }>("/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  detail: (id: string) => request<Detail>("/conversations/" + id),
  update: (id: string, data: object) =>
    request("/conversations/" + id, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  send: (id: string, data: object) =>
    request<{ message: Message; run: Run }>(
      "/conversations/" + id + "/messages",
      { method: "POST", body: JSON.stringify(data) },
    ),
  action: (id: number, action: string, budget?: object) =>
    request<{ run: Run }>("/runs/" + id + "/actions", {
      method: "POST",
      body: JSON.stringify({ action, budget, request_id: crypto.randomUUID() }),
    }),
  run: (id: number) => request<{ run: Run; tools: any[] }>("/runs/" + id),
  region: (data: object) =>
    request<{ attachment: Attachment }>("/attachments", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  window: (id: string, data: object) =>
    request<any>("/attachments/" + id + "/windows", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  capabilities: () => request<Capabilities>("/capabilities"),
  places: (q: string) => request<any>("/places?q=" + encodeURIComponent(q)),
  async upload(
    file: File,
    conversation_id?: string,
    onProgress?: (fraction: number) => void,
  ) {
    const identity =
      "ss-upload:" +
      conversation_id +
      ":" +
      file.name +
      ":" +
      file.size +
      ":" +
      file.lastModified;
    let up: { id: string; chunk_size: number; received_chunks: number[] };
    const saved = sessionStorage.getItem(identity);
    try {
      if (!saved) throw new Error();
      up = await request("/uploads/" + saved);
    } catch {
      up = await request("/uploads", {
        method: "POST",
        body: JSON.stringify({
          name: file.name,
          size_bytes: file.size,
          conversation_id,
        }),
      });
      sessionStorage.setItem(identity, up.id);
    }
    const count = Math.ceil(file.size / up.chunk_size);
    for (let i = 0; i < count; i++) {
      if (!up.received_chunks.includes(i)) {
        const r = await fetch(api + "/uploads/" + up.id + "/chunks/" + i, {
          credentials: "same-origin",
          method: "PUT",
          headers: { "X-CSRFToken": csrf() },
          body: file.slice(i * up.chunk_size, (i + 1) * up.chunk_size),
        });
        if (!r.ok) throw new Error("上传中断，重新选择同一文件可继续");
      }
      onProgress?.((i + 1) / count);
    }
    const result = await request<{ attachment: Attachment }>(
      "/uploads/" + up.id + "/complete",
      { method: "POST" },
    );
    sessionStorage.removeItem(identity);
    return result;
  },
};
export function eventStream(
  id: string,
  after: number,
  onEvent: (e: any) => void,
  onStatus?: (connected: boolean) => void,
) {
  let stopped = false,
    last = after,
    timer = 0,
    source: EventSource | undefined;
  function connect() {
    if (stopped) return;
    source = new EventSource(
      api + "/conversations/" + id + "/events?after=" + last,
    );
    source.onopen = () => onStatus?.(true);
    source.onmessage = (e) => {
      try {
        const x = JSON.parse(e.data);
        if (x.sequence <= last) return;
        last = x.sequence;
        onEvent(x);
      } catch {}
    };
    source.onerror = () => {
      onStatus?.(false);
      source?.close();
      if (!stopped) timer = window.setTimeout(connect, 1500);
    };
  }
  connect();
  return () => {
    stopped = true;
    clearTimeout(timer);
    source?.close();
  };
}
