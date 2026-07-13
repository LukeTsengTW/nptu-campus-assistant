import type { ChatResponse } from "@nptu/shared";


export type ChatRequestMessage = {
  type: "NPTU_CHAT_REQUEST";
  question: string;
  conversationId?: string;
};

export type ChatClearMessage = {
  type: "NPTU_CHAT_CLEAR";
  conversationId: string;
};

export type ChatResultMessage =
  | { ok: true; data: ChatResponse }
  | { ok: false; error: string };

export type ChatClearResultMessage =
  | { ok: true }
  | { ok: false; error: string };


export async function sendChatMessage(
  question: string,
  conversationId?: string,
): Promise<ChatResponse> {
  const result = await browser.runtime.sendMessage<ChatRequestMessage, ChatResultMessage>({
    type: "NPTU_CHAT_REQUEST",
    question,
    ...(conversationId ? { conversationId } : {}),
  });
  if (!result?.ok) {
    throw new Error(result?.error || "無法連線到後端服務。");
  }
  return result.data;
}


export async function clearChatConversation(conversationId: string): Promise<void> {
  const result = await browser.runtime.sendMessage<ChatClearMessage, ChatClearResultMessage>({
    type: "NPTU_CHAT_CLEAR",
    conversationId,
  });
  if (!result?.ok) {
    throw new Error(result?.error || "無法清除伺服器對話狀態。")
  }
}
