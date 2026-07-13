import { ApiClient } from "../src/lib/api-client";
import type {
  ChatClearMessage,
  ChatClearResultMessage,
  ChatRequestMessage,
  ChatResultMessage,
} from "../src/lib/messages";


const apiBaseUrl = import.meta.env.WXT_API_BASE_URL ?? "http://127.0.0.1:8000";
const api = new ApiClient(apiBaseUrl);


export default defineBackground(() => {
  browser.runtime.onMessage.addListener((message: unknown) => {
    const clear = message as Partial<ChatClearMessage>;
    if (clear.type === "NPTU_CHAT_CLEAR" && typeof clear.conversationId === "string") {
      return api
        .deleteConversation(clear.conversationId)
        .then<ChatClearResultMessage>(() => ({ ok: true }))
        .catch<ChatClearResultMessage>((error: unknown) => ({
          ok: false,
          error: error instanceof Error ? error.message : "無法清除伺服器對話狀態。",
        }));
    }
    const request = message as Partial<ChatRequestMessage>;
    if (request.type !== "NPTU_CHAT_REQUEST" || typeof request.question !== "string") {
      return undefined;
    }
    return api
      .chat(request.question, request.conversationId)
      .then<ChatResultMessage>((data) => ({ ok: true, data }))
      .catch<ChatResultMessage>((error: unknown) => ({
        ok: false,
        error: error instanceof Error ? error.message : "無法連線到後端服務。",
      }));
  });
});
