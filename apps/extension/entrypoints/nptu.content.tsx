import React from "react";
import ReactDOM from "react-dom/client";

import { ChatWidget } from "../src/components/ChatWidget";
import "../src/styles.css";


export default defineContentScript({
  matches: ["https://nptu.edu.tw/*", "https://*.nptu.edu.tw/*"],
  cssInjectionMode: "ui",
  async main(ctx) {
    const ui = await createShadowRootUi(ctx, {
      name: "nptu-campus-assistant",
      position: "overlay",
      anchor: "body",
      isolateEvents: true,
      onMount(container) {
        const root = ReactDOM.createRoot(container);
        root.render(
          <React.StrictMode>
            <ChatWidget />
          </React.StrictMode>,
        );
        return root;
      },
      onRemove(root) {
        root?.unmount();
      },
    });
    ui.mount();
  },
});
