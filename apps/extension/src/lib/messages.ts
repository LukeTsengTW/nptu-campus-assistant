import type { ChatResponse } from "@nptu/shared";


export type ChatRequestMessage = {
  type: "NPTU_CHAT_REQUEST";
  question: string;
};

export type ChatResultMessage =
  | { ok: true; data: ChatResponse }
  | { ok: false; error: string };


export async function sendChatMessage(question: string): Promise<ChatResponse> {
  const result = await browser.runtime.sendMessage<ChatRequestMessage, ChatResultMessage>({
    type: "NPTU_CHAT_REQUEST",
    question,
  });
  if (!result?.ok) {
    throw new Error(result?.error || "無法連線到後端服務。");
  }
  return result.data;
}
