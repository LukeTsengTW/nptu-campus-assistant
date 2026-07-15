import type { ChatResponse } from "@nptu/shared";
import type { ReactNode } from "react";
import { FormEvent, useEffect, useId, useRef, useState } from "react";

import { clearChatConversation, sendChatMessage } from "../lib/messages";
import {
  clearConversationId,
  loadConversationId,
  loadPanelOpen,
  saveConversationId,
  savePanelOpen,
} from "../lib/storage";


type ChatEntry =
  | { id: number; role: "user"; text: string }
  | { id: number; role: "assistant"; response: ChatResponse };

type Props = {
  sendQuestion?: (question: string, conversationId?: string) => Promise<ChatResponse>;
  clearConversation?: (conversationId: string) => Promise<void>;
  initialOpen?: boolean;
};


function renderAnswer(answer: string): ReactNode {
  const linkPattern = /\[([^\]\r\n]+)\]\((https?:\/\/[^\s)]+)\)/g;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = linkPattern.exec(answer)) !== null) {
    if (match.index > cursor) {
      nodes.push(answer.slice(cursor, match.index));
    }
    nodes.push(
      <a
        className="answer-link"
        href={match[2]}
        key={`answer-link-${match.index}`}
        target="_blank"
        rel="noopener noreferrer"
      >
        {match[1]}
      </a>,
    );
    cursor = match.index + match[0].length;
  }

  if (cursor < answer.length) {
    nodes.push(answer.slice(cursor));
  }
  return nodes.length > 0 ? nodes : answer;
}


export function ChatWidget({
  sendQuestion = sendChatMessage,
  clearConversation = clearChatConversation,
  initialOpen,
}: Props) {
  const [open, setOpen] = useState(initialOpen ?? false);
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<ChatEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const inputId = useId();
  const requestGeneration = useRef(0);

  useEffect(() => {
    if (initialOpen === undefined) {
      void loadPanelOpen().then(setOpen).catch(() => undefined);
    }
    void loadConversationId().then(setConversationId).catch(() => undefined);
  }, [initialOpen]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    void savePanelOpen(next).catch(() => undefined);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || loading) return;
    const userEntry: ChatEntry = { id: Date.now(), role: "user", text: trimmed };
    setMessages((current) => [...current, userEntry]);
    setQuestion("");
    setError(null);
    setLoading(true);
    const generation = ++requestGeneration.current;
    try {
      const response = await sendQuestion(trimmed, conversationId ?? undefined);
      if (requestGeneration.current !== generation) return;
      setConversationId(response.conversation_id);
      void saveConversationId(response.conversation_id).catch(() => undefined);
      setMessages((current) => [
        ...current,
        { id: Date.now() + 1, role: "assistant", response },
      ]);
    } catch (reason) {
      if (requestGeneration.current !== generation) return;
      setError(reason instanceof Error ? reason.message : "查詢失敗，請稍後再試。");
    } finally {
      if (requestGeneration.current === generation) setLoading(false);
    }
  };

  const clear = () => {
    requestGeneration.current += 1;
    setMessages([]);
    setError(null);
    setQuestion("");
    setLoading(false);
    const currentConversationId = conversationId;
    setConversationId(null);
    void clearConversationId().catch(() => undefined);
    if (currentConversationId) {
      void clearConversation(currentConversationId).catch(() => undefined);
    }
  };

  return (
    <div className="nptu-assistant-root">
      {open && (
        <section className="assistant-panel" aria-label="NPTU 校務資訊助理聊天視窗">
          <header className="assistant-header">
            <div>
              <div className="assistant-eyebrow"><span>非官方</span> NPTU CAMPUS GUIDE</div>
              <h1>校務資訊助理</h1>
            </div>
            <button className="icon-button" type="button" onClick={toggle} aria-label="關閉聊天視窗">
              ×
            </button>
          </header>

          <div className="assistant-thread" aria-live="polite">
            {messages.length === 0 && !error && (
              <div className="welcome-card">
                <div className="welcome-mark">查</div>
                <h2>從官方資料開始找答案</h2>
                <p>可詢問校務辦法、申請資格，或搜尋近期公告與截止日。</p>
              </div>
            )}
            {messages.map((message) =>
              message.role === "user" ? (
                <div className="message-row user-row" key={message.id}>
                  <div className="message-bubble user-bubble">{message.text}</div>
                </div>
              ) : (
                <article className="answer-card" key={message.id}>
                  <div className="answer-label">AI 整理說明</div>
                  <p>{renderAnswer(message.response.answer)}</p>
                  {message.response.warning && (
                    <div className="warning-box">{message.response.warning}</div>
                  )}
                  {message.response.sources.length > 0 && (
                    <div className="sources-block">
                      <h3>官方來源</h3>
                      {message.response.sources.map((source) => (
                        <a
                          className="source-card"
                          href={source.url}
                          key={`${message.id}-${source.url}`}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <span className="source-title">{source.title}</span>
                          <span className="source-meta">
                            {source.unit} · {source.published_at ?? "日期未提供"} · 官方
                          </span>
                        </a>
                      ))}
                    </div>
                  )}
                </article>
              ),
            )}
            {loading && <div className="loading-row"><span />正在查詢官方資料…</div>}
            {error && <div className="error-box" role="alert">{error}</div>}
          </div>

          <form className="assistant-composer" onSubmit={submit}>
            <label htmlFor={inputId}>輸入校務問題</label>
            <textarea
              id={inputId}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              maxLength={2000}
              rows={3}
              placeholder="例如：最近有哪些獎學金公告？"
            />
            <div className="composer-actions">
              <button
                className="clear-button"
                type="button"
                onClick={clear}
                disabled={messages.length === 0 && !error}
              >
                清除對話
              </button>
              <button className="send-button" type="submit" disabled={!question.trim() || loading}>
                送出問題 <span aria-hidden="true">↗</span>
              </button>
            </div>
          </form>

          <footer className="assistant-disclaimer">
            本工具並非國立屏東大學官方系統。重要申請資格、期限與規定請以原始官方公告為準。
          </footer>
        </section>
      )}

      {!open && (
        <button className="assistant-launcher" type="button" onClick={toggle} aria-label="開啟 NPTU 校務資訊助理">
          <span className="launcher-glyph">問</span>
          <span className="launcher-copy"><strong>校務助理</strong><small>官方資料查詢</small></span>
        </button>
      )}
    </div>
  );
}
