const PANEL_OPEN_KEY = "nptuAssistantPanelOpen";


export async function loadPanelOpen(): Promise<boolean> {
  const result = await browser.storage.local.get(PANEL_OPEN_KEY);
  return result[PANEL_OPEN_KEY] === true;
}


export async function savePanelOpen(open: boolean): Promise<void> {
  await browser.storage.local.set({ [PANEL_OPEN_KEY]: open });
}
