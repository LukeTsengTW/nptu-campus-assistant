const PANEL_OPEN_KEY = "nptuAssistantPanelOpen";
const CONVERSATION_ID_KEY = "nptuAssistantConversationId";


export async function loadPanelOpen(): Promise<boolean> {
  const result = await browser.storage.local.get(PANEL_OPEN_KEY);
  return result[PANEL_OPEN_KEY] === true;
}


export async function savePanelOpen(open: boolean): Promise<void> {
  await browser.storage.local.set({ [PANEL_OPEN_KEY]: open });
}


export async function loadConversationId(): Promise<string | null> {
  const result = await browser.storage.local.get(CONVERSATION_ID_KEY);
  return typeof result[CONVERSATION_ID_KEY] === "string"
    ? result[CONVERSATION_ID_KEY]
    : null;
}


export async function saveConversationId(conversationId: string): Promise<void> {
  await browser.storage.local.set({ [CONVERSATION_ID_KEY]: conversationId });
}


export async function clearConversationId(): Promise<void> {
  await browser.storage.local.remove(CONVERSATION_ID_KEY);
}
