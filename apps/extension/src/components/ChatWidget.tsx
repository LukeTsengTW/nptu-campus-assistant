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

const suggestedQuestions = [
  {
    label: "近期公告",
    description: "查看最新校務消息",
    prompt: "查詢近期最新公告",
  },
  {
    label: "獎學金資訊",
    description: "查詢校內外獎助學金",
    prompt: "查詢獎學金公告",
  },
  {
    label: "選課公告",
    description: "查看選課與課務通知",
    prompt: "查詢選課公告",
  },
] as const;


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

  const submitQuestion = async (value: string) => {
    const trimmed = value.trim();
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

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void submitQuestion(question);
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
        <section
          className="assistant-panel"
          aria-label="NPTU 校務資訊助理聊天視窗"
        >
          <header className="assistant-header">
            <div className="assistant-header-main">
              <div className="assistant-brand-mark" aria-hidden="true">
                <span>N</span>
                <span className="brand-mark-accent" />
              </div>
              <div className="assistant-title-group">
                <h1>校務資訊助理</h1>
                <p>非官方校務資訊查詢工具</p>
              </div>
            </div>
            <button className="icon-button" type="button" onClick={toggle} aria-label="關閉聊天視窗">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="m5 5 10 10M15 5 5 15" />
              </svg>
            </button>
          </header>

          <div className="assistant-thread" aria-live="polite">
            {messages.length === 0 && !error && (
              <>
                <div className="welcome-section">
                  <div className="section-kicker">
                    <span aria-hidden="true" />
                    快速查詢
                  </div>
                  <h2>查詢屏大校務資訊</h2>
                  <p>可查詢近期公告、獎學金、選課資訊與校務規定。回答會附上可供核對的官方來源。</p>
                </div>
                <div className="suggestion-list" role="group" aria-label="建議問題">
                  {suggestedQuestions.map((suggestion) => (
                    <button
                      className="suggestion-item"
                      type="button"
                      key={suggestion.prompt}
                      onClick={() => void submitQuestion(suggestion.prompt)}
                      disabled={loading}
                    >
                      <span className="suggestion-content">
                        <strong>{suggestion.label}</strong>
                        <small>{suggestion.description}</small>
                      </span>
                      <svg className="suggestion-arrow" viewBox="0 0 16 16" aria-hidden="true">
                        <path d="m6 3 5 5-5 5" />
                      </svg>
                    </button>
                  ))}
                </div>
              </>
            )}
            {messages.map((message) =>
              message.role === "user" ? (
                <div className="message-row user-row" key={message.id}>
                  <div className="message-bubble user-bubble">{message.text}</div>
                </div>
              ) : (
                <article className="answer-entry" key={message.id}>
                  <div className="answer-label">查詢結果</div>
                  <p className="answer-copy">{renderAnswer(message.response.answer)}</p>
                  {message.response.warning && (
                    <div className="warning-box">{message.response.warning}</div>
                  )}
                  {message.response.sources.length > 0 && (
                    <div className="sources-block">
                      <h3>官方來源</h3>
                      {message.response.sources.map((source) => (
                        <a
                          className="source-item"
                          href={source.url}
                          key={`${message.id}-${source.url}`}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <span className="source-title">{source.title}</span>
                          <span className="source-meta">
                            {source.unit} · {source.published_at ?? "日期未提供"}
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
              rows={2}
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
              <button
                className="send-button"
                type="submit"
                aria-label="送出問題"
                disabled={!question.trim() || loading}
              >
                送出
              </button>
            </div>
          </form>

          <footer className="assistant-disclaimer">
            <span className="disclaimer-dot" aria-hidden="true" />
            <span>本工具並非國立屏東大學官方系統。重要申請資格、期限與規定請以原始官方公告為準。</span>
          </footer>
        </section>
      )}

      {!open && (
        <button
          className="assistant-launcher"
          type="button"
          onClick={toggle}
          aria-label="開啟 NPTU 校務資訊助理"
          title="校務資訊助理"
        >
          <svg className="launcher-icon" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5.25 6.75h13.5A1.75 1.75 0 0 1 20.5 8.5v7A1.75 1.75 0 0 1 18.75 17h-7.2l-3.8 2.75V17h-2.5A1.75 1.75 0 0 1 3.5 15.25v-6a2.5 2.5 0 0 1 1.75-2.5Z" />
            <path d="M9.25 11.75h.01M12 11.75h.01M14.75 11.75h.01" />
          </svg>
        </button>
      )}
    </div>
  );
}
