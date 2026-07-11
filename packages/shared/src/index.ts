import type { components, paths } from "./schema";

export type ChatRequest = components["schemas"]["ChatRequest"];
export type ChatResponse = components["schemas"]["ChatResponse"];
export type SourceReference = components["schemas"]["SourceReference"];
export type AnnouncementItem = components["schemas"]["AnnouncementItem"];
export type AnnouncementListResponse = components["schemas"]["AnnouncementListResponse"];
export type IngestionSummary = components["schemas"]["IngestionSummary"];
export type CrawlSummary = components["schemas"]["CrawlSummary"];
export type ErrorResponse = components["schemas"]["ErrorResponse"];
export type ApiPaths = paths;
