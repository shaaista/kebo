const SCRAPER_BASE = "/kb-scraper/api";

export interface ScrapeRequest {
  url: string;
  project_name?: string;
  bot_enabled?: boolean;
  language?: string;
  specific_urls?: string[];
  output_mode?: "both" | "kb_only" | "media_only";
}

export interface JobSummary {
  job_id: string;
  session_name: string;
  url: string;
  status: string;
  queue_state: string;
  task_type: string;
  progress_pct: number;
  progress_msg: string;
  can_stop: boolean;
  can_resume: boolean;
  can_open_review: boolean;
  can_download: boolean;
  output_dir: string;
  created_at: string;
  completed_at: string | null;
}

export interface JobStatus extends JobSummary {
  pages_found: number;
  pages_crawled: number;
  pages_failed: number;
  properties_found: number;
  kb_preview: string;
  error_message: string;
}

export interface ReviewItem {
  id: string;
  type: string;
  name?: string;
  suggested_name?: string;
  label?: string;
  suggested_label?: string;
  enabled?: boolean;
  url?: string;
  source_page_item_id?: string;
}

export interface ReviewEntity {
  id: string;
  name?: string;
  suggested_name?: string;
  enabled?: boolean;
  stats?: { pages?: number };
  items: ReviewItem[];
}

export interface ReviewPayload {
  project?: { name?: string };
  entities: ReviewEntity[];
}

export interface ReviewResponse {
  job_id: string;
  status: string;
  review_data: ReviewPayload;
}

async function scraperFetch(path: string, options: RequestInit = {}, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${SCRAPER_BASE}${path}`, {
      ...options,
      signal: controller.signal,
    });
    const text = await response.text();
    if (!text) return null;
    let payload: unknown;
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
    if (!response.ok) {
      const err = payload as Record<string, unknown>;
      const detail =
        (typeof err?.detail === "string" ? err.detail : null) ||
        response.statusText ||
        `HTTP ${response.status}`;
      throw new Error(detail);
    }
    return payload;
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new Error("KB scraper did not respond in time");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export const scraperApi = {
  startScrape: (req: ScrapeRequest) =>
    scraperFetch("/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    }),

  listJobs: (): Promise<JobSummary[]> =>
    scraperFetch("/jobs") as Promise<JobSummary[]>,

  getStatus: (jobId: string): Promise<JobStatus> =>
    scraperFetch(`/status/${jobId}`) as Promise<JobStatus>,

  getReview: (jobId: string): Promise<ReviewResponse> =>
    scraperFetch(`/review/${jobId}`) as Promise<ReviewResponse>,

  publish: (jobId: string, reviewData: ReviewPayload) =>
    scraperFetch(`/publish/${jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_data: reviewData }),
    }),

  stop: (jobId: string) =>
    scraperFetch(`/jobs/${jobId}/stop`, { method: "POST" }),

  resume: (jobId: string) =>
    scraperFetch(`/jobs/${jobId}/resume`, { method: "POST" }),

  remove: (jobId: string) =>
    scraperFetch(`/jobs/${jobId}`, { method: "DELETE" }),

  downloadUrl: (jobId: string) => `${SCRAPER_BASE}/download/${jobId}`,
};
