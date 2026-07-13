import type { ChatResponse } from "@nptu/shared";


type ErrorEnvelope = { error?: { message?: unknown } };


const ANSWER_TYPES = new Set(["official_document", "announcement", "insufficient_information"]);
const CONFIDENCE_LEVELS = new Set(["high", "medium", "low"]);
const NETWORK_ERROR_MESSAGE = "無法連線到後端服務，請確認本機 API 已啟動。";


function isOfficialNptuUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/\.$/, "");
    return url.protocol === "https:" && (host === "nptu.edu.tw" || host.endsWith(".nptu.edu.tw"));
  } catch {
    return false;
  }
}


function isSourceReference(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const source = value as Record<string, unknown>;
  return (
    typeof source.id === "string" &&
    typeof source.kind === "string" && ["official_document", "announcement"].includes(source.kind) &&
    typeof source.title === "string" &&
    typeof source.unit === "string" &&
    isOfficialNptuUrl(source.url) &&
    source.source_type === "official" &&
    (source.published_at === null || typeof source.published_at === "string")
  );
}


function isChatResponse(value: unknown): value is ChatResponse {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.conversation_id === "string" &&
    typeof record.answer === "string" &&
    typeof record.answer_type === "string" && ANSWER_TYPES.has(record.answer_type) &&
    typeof record.confidence === "string" && CONFIDENCE_LEVELS.has(record.confidence) &&
    Array.isArray(record.sources) && record.sources.every(isSourceReference) &&
    (record.warning === null || typeof record.warning === "string")
  );
}


export class ApiClient {
  private readonly baseUrl: string;

  constructor(baseUrl: string, private readonly timeoutMs = 15_000) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  async chat(question: string, conversationId?: string): Promise<ChatResponse> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}/v1/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          ...(conversationId ? { conversation_id: conversationId } : {}),
        }),
        signal: controller.signal,
      });
      const payload: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        const envelope = payload as ErrorEnvelope | null;
        const message = envelope?.error?.message;
        throw new Error(typeof message === "string" ? message : "後端回傳無法處理的錯誤。");
      }
      if (!isChatResponse(payload)) {
        throw new Error("後端回應格式不正確。");
      }
      return payload;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new Error("查詢逾時，請稍後再試。");
      }
      if (error instanceof TypeError && error.message === "Failed to fetch") {
        throw new Error(NETWORK_ERROR_MESSAGE);
      }
      if (error instanceof Error) throw error;
      throw new Error("無法連線到後端服務。");
    } finally {
      clearTimeout(timeout);
    }
  }

  async deleteConversation(conversationId: string): Promise<void> {
    const response = await fetch(
      `${this.baseUrl}/v1/conversations/${encodeURIComponent(conversationId)}`,
      { method: "DELETE" },
    );
    if (!response.ok) {
      throw new Error("無法清除伺服器對話狀態。")
    }
  }
}
