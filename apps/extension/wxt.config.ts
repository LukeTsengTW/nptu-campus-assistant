import { defineConfig } from "wxt";

const apiBaseUrl = process.env.WXT_API_BASE_URL ?? "http://127.0.0.1:8000";
const apiOrigin = new URL(apiBaseUrl).origin;

export default defineConfig({
  modules: ["@wxt-dev/module-react"],
  manifest: {
    name: "NPTU 校務資訊助理",
    description: "非官方的國立屏東大學官方文件與公告查詢工具。",
    version: "0.1.0",
    permissions: ["storage"],
    host_permissions: [`${apiOrigin}/*`],
  },
});
