import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/lib/api-client";


describe("ApiClient", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("送出 typed chat request 並解析成功回應", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        answer: "回答",
        answer_type: "insufficient_information",
        confidence: "low",
        sources: [],
        warning: null,
      }),
    }));

    const result = await new ApiClient("http://127.0.0.1:8000").chat("測試");

    expect(result.answer).toBe("回答");
    expect(fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/v1/chat",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("將非 2xx error envelope 轉為安全錯誤訊息", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      json: vi.fn().mockResolvedValue({ error: { message: "請求過於頻繁" } }),
    }));

    await expect(new ApiClient("http://127.0.0.1:8000").chat("測試")).rejects.toThrow(
      "請求過於頻繁",
    );
  });

  it("將瀏覽器 fetch 連線失敗轉成可操作的繁體中文錯誤", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    await expect(new ApiClient("http://127.0.0.1:8000").chat("測試")).rejects.toThrow(
      "無法連線到後端服務，請確認本機 API 已啟動。",
    );
  });

  it("rejects an invalid answer type and unsafe source URL", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        answer: "不可信回應",
        answer_type: "made_up",
        confidence: "high",
        sources: [{
          title: "外部網站",
          url: "https://example.com/phishing",
          unit: "未知",
          published_at: null,
          source_type: "official",
        }],
        warning: null,
      }),
    }));

    await expect(new ApiClient("http://127.0.0.1:8000").chat("測試")).rejects.toThrow(
      "回應格式不正確",
    );
  });
});
