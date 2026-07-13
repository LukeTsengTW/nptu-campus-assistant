import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ChatResponse } from "@nptu/shared";
import { describe, expect, it, vi } from "vitest";

import { ChatWidget } from "../src/components/ChatWidget";


const response = {
  conversation_id: "conversation-1",
  answer: "申請期限請依公告辦理。",
  answer_type: "announcement" as const,
  confidence: "high" as const,
  warning: null,
  sources: [
    {
      id: "announcement-1",
      kind: "announcement" as const,
      title: "115學年度申請公告",
      url: "https://www.nptu.edu.tw/announcement",
      unit: "教務處",
      published_at: "2026-07-10",
      source_type: "official",
    },
  ],
} satisfies ChatResponse;


describe("ChatWidget", () => {
  it("渲染懸浮按鈕並開啟含免責聲明的聊天視窗", async () => {
    const user = userEvent.setup();
    render(<ChatWidget sendQuestion={vi.fn()} initialOpen={false} />);

    expect(screen.getByRole("button", { name: "開啟 NPTU 校務資訊助理" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "開啟 NPTU 校務資訊助理" }));

    expect(screen.getByRole("heading", { name: "校務資訊助理" })).toBeVisible();
    expect(screen.getByText(/本工具並非國立屏東大學官方系統/)).toBeVisible();
  });

  it("送出問題、顯示載入狀態、回答與官方來源", async () => {
    const user = userEvent.setup();
    let resolveResponse: (value: typeof response) => void = () => undefined;
    const sendQuestion = vi.fn(
      () => new Promise<typeof response>((resolve) => { resolveResponse = resolve; }),
    );
    render(<ChatWidget sendQuestion={sendQuestion} initialOpen />);

    await user.type(screen.getByLabelText("輸入校務問題"), "最近有哪些申請公告？");
    await user.click(screen.getByRole("button", { name: "送出問題" }));

    expect(screen.getByText("正在查詢官方資料…")).toBeVisible();
    resolveResponse(response);
    await waitFor(() => expect(screen.getByText(response.answer)).toBeVisible());
    expect(screen.getByRole("link", { name: /115學年度申請公告/ })).toHaveAttribute(
      "href",
      "https://www.nptu.edu.tw/announcement",
    );
  });

  it("顯示 API 錯誤並可清除對話", async () => {
    const user = userEvent.setup();
    const sendQuestion = vi.fn().mockRejectedValue(new Error("後端無法連線"));
    render(<ChatWidget sendQuestion={sendQuestion} initialOpen />);

    await user.type(screen.getByLabelText("輸入校務問題"), "測試問題");
    await user.click(screen.getByRole("button", { name: "送出問題" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("後端無法連線");

    await user.click(screen.getByRole("button", { name: "清除對話" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("清除對話後忽略仍在途的舊回應", async () => {
    const user = userEvent.setup();
    let resolveResponse: (value: typeof response) => void = () => undefined;
    const sendQuestion = vi.fn(
      () => new Promise<typeof response>((resolve) => { resolveResponse = resolve; }),
    );
    render(<ChatWidget sendQuestion={sendQuestion} initialOpen />);

    await user.type(screen.getByLabelText("輸入校務問題"), "測試問題");
    await user.click(screen.getByRole("button", { name: "送出問題" }));
    await user.click(screen.getByRole("button", { name: "清除對話" }));
    resolveResponse(response);

    await waitFor(() => expect(screen.queryByText("正在查詢官方資料…")).not.toBeInTheDocument());
    expect(screen.queryByText(response.answer)).not.toBeInTheDocument();
  });

  it("後續問題重送 conversation id，清除時刪除 server state", async () => {
    const user = userEvent.setup();
    const sendQuestion = vi.fn().mockResolvedValue(response);
    const clearConversation = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatWidget
        sendQuestion={sendQuestion}
        clearConversation={clearConversation}
        initialOpen
      />,
    );

    await user.type(screen.getByLabelText("輸入校務問題"), "最近公告");
    await user.click(screen.getByRole("button", { name: "送出問題" }));
    await screen.findByText(response.answer);
    await user.type(screen.getByLabelText("輸入校務問題"), "第三則");
    await user.click(screen.getByRole("button", { name: "送出問題" }));

    await waitFor(() => expect(sendQuestion).toHaveBeenLastCalledWith("第三則", "conversation-1"));
    await user.click(screen.getByRole("button", { name: "清除對話" }));
    expect(clearConversation).toHaveBeenCalledWith("conversation-1");
  });
});
