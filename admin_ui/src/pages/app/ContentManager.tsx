import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Download,
  Loader2,
  RefreshCcw,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  scraperApi,
  type JobSummary,
  type ReviewEntity,
  type ReviewItem,
  type ReviewPayload,
} from "@/lib/scraperApi";

function statusLabel(status: string) {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function itemDisplayName(item: ReviewItem) {
  return item.name || item.suggested_name || item.url || item.id;
}

function entityDisplayName(entity: ReviewEntity) {
  return entity.name || entity.suggested_name || entity.id;
}

function EntityBlock({
  entity,
  onChange,
}: {
  entity: ReviewEntity;
  onChange: (updated: ReviewEntity) => void;
}) {
  const [expanded, setExpanded] = useState(true);

  const pages = entity.items.filter((i) => i.type === "page");
  const assets = entity.items.filter((i) => i.type !== "page");

  function toggleEntity(checked: boolean) {
    onChange({
      ...entity,
      enabled: checked,
      items: entity.items.map((i) => ({ ...i, enabled: checked })),
    });
  }

  function toggleItem(id: string, checked: boolean) {
    onChange({
      ...entity,
      items: entity.items.map((i) =>
        i.id === id ? { ...i, enabled: checked } : i
      ),
    });
  }

  const enabledCount = pages.filter((p) => p.enabled !== false).length;

  return (
    <Card className="mb-3">
      <CardHeader className="py-3 px-4">
        <div className="flex items-center gap-3">
          <Checkbox
            id={`entity-${entity.id}`}
            checked={entity.enabled !== false}
            onCheckedChange={(v) => toggleEntity(!!v)}
          />
          <Label
            htmlFor={`entity-${entity.id}`}
            className="text-base font-semibold cursor-pointer flex-1"
          >
            {entityDisplayName(entity)}
          </Label>
          <span className="text-xs text-muted-foreground">
            {enabledCount}/{pages.length} pages
          </span>
          <button
            onClick={() => setExpanded((v) => !v)}
            className="text-muted-foreground"
          >
            {expanded ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </button>
        </div>
      </CardHeader>

      {expanded && (
        <CardContent className="pt-0 px-4 pb-4">
          {pages.length === 0 ? (
            <p className="text-sm text-muted-foreground">No pages found.</p>
          ) : (
            <ul className="space-y-1.5">
              {pages.map((page) => (
                <li key={page.id} className="flex items-start gap-2">
                  <Checkbox
                    id={`page-${page.id}`}
                    checked={page.enabled !== false}
                    onCheckedChange={(v) => toggleItem(page.id, !!v)}
                    className="mt-0.5"
                  />
                  <Label
                    htmlFor={`page-${page.id}`}
                    className="text-sm cursor-pointer"
                  >
                    <span className="font-medium">{itemDisplayName(page)}</span>
                    {page.url && (
                      <span className="block text-xs text-muted-foreground truncate max-w-sm">
                        {page.url}
                      </span>
                    )}
                  </Label>
                </li>
              ))}
            </ul>
          )}
          {assets.length > 0 && (
            <p className="text-xs text-muted-foreground mt-2">
              +{assets.length} media asset{assets.length !== 1 ? "s" : ""}
            </p>
          )}
        </CardContent>
      )}
    </Card>
  );
}

export default function ContentManager() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [loadingJobs, setLoadingJobs] = useState(true);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  const [reviewPayload, setReviewPayload] = useState<ReviewPayload | null>(null);
  const [loadingReview, setLoadingReview] = useState(false);
  const [publishStatus, setPublishStatus] = useState<"idle" | "busy" | "ok" | "err">("idle");
  const [publishMsg, setPublishMsg] = useState("");

  const fetchJobs = useCallback(async () => {
    try {
      const list = await scraperApi.listJobs();
      setJobs(list.filter((j) => j.can_open_review || j.status === "completed"));
    } catch {
      // ignore
    } finally {
      setLoadingJobs(false);
    }
  }, []);

  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  async function loadReview(jobId: string) {
    setSelectedJobId(jobId);
    setReviewPayload(null);
    setPublishStatus("idle");
    setPublishMsg("");
    setLoadingReview(true);
    try {
      const res = await scraperApi.getReview(jobId);
      setReviewPayload(res.review_data);
    } catch (e) {
      setPublishMsg(e instanceof Error ? e.message : "Failed to load review data");
      setPublishStatus("err");
    } finally {
      setLoadingReview(false);
    }
  }

  function updateEntity(updated: ReviewEntity) {
    if (!reviewPayload) return;
    setReviewPayload({
      ...reviewPayload,
      entities: reviewPayload.entities.map((e) =>
        e.id === updated.id ? updated : e
      ),
    });
  }

  async function handlePublish() {
    if (!selectedJobId || !reviewPayload) return;
    setPublishStatus("busy");
    setPublishMsg("");
    try {
      await scraperApi.publish(selectedJobId, reviewPayload);
      setPublishStatus("ok");
      setPublishMsg("Publishing started. Check the Web Crawl page for progress.");
      await fetchJobs();
    } catch (e) {
      setPublishStatus("err");
      setPublishMsg(e instanceof Error ? e.message : "Publish failed");
    }
  }

  const selectedJob = jobs.find((j) => j.job_id === selectedJobId);

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-bold">Content Manager</h1>
        <p className="text-muted-foreground">
          Review extracted content and publish your knowledge base.
        </p>
      </div>

      {/* Job selector */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">Select a Session to Review</CardTitle>
            <Button variant="ghost" size="sm" onClick={fetchJobs}>
              <RefreshCcw className="h-3.5 w-3.5" />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {loadingJobs ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading sessions…
            </div>
          ) : jobs.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No reviewable sessions yet. Start a crawl in Web Crawling first.
            </p>
          ) : (
            <ul className="space-y-2">
              {jobs.map((job) => (
                <li key={job.job_id}>
                  <button
                    className={`w-full text-left rounded-lg border px-4 py-3 transition-colors ${
                      selectedJobId === job.job_id
                        ? "border-primary bg-primary/5"
                        : "hover:bg-muted/50"
                    }`}
                    onClick={() => loadReview(job.job_id)}
                  >
                    <div className="font-medium text-sm">
                      {job.session_name || "Unnamed Session"}
                    </div>
                    <div className="text-xs text-muted-foreground truncate">
                      {job.url} · {statusLabel(job.status)}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* Review panel */}
      {selectedJobId && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-semibold">
              Review:{" "}
              {selectedJob?.session_name || selectedJobId.slice(0, 8)}
            </h2>
            {selectedJob?.can_download && (
              <Button variant="outline" size="sm" asChild>
                <a href={scraperApi.downloadUrl(selectedJobId)} download>
                  <Download className="h-3.5 w-3.5 mr-1" />
                  Download ZIP
                </a>
              </Button>
            )}
          </div>

          {loadingReview ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading review data…
            </div>
          ) : reviewPayload ? (
            <>
              {reviewPayload.entities.map((entity) => (
                <EntityBlock
                  key={entity.id}
                  entity={entity}
                  onChange={updateEntity}
                />
              ))}

              <div className="mt-4 flex items-center gap-3">
                <Button
                  onClick={handlePublish}
                  disabled={publishStatus === "busy"}
                >
                  {publishStatus === "busy" ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : null}
                  Publish to Knowledge Base
                </Button>

                {publishStatus === "ok" && (
                  <div className="flex items-center gap-1.5 text-sm text-green-600">
                    <CheckCircle2 className="h-4 w-4" />
                    {publishMsg}
                  </div>
                )}
                {publishStatus === "err" && (
                  <div className="flex items-center gap-1.5 text-sm text-red-600">
                    <AlertTriangle className="h-4 w-4" />
                    {publishMsg}
                  </div>
                )}
              </div>
            </>
          ) : publishStatus === "err" ? (
            <div className="flex items-center gap-2 text-sm text-red-600">
              <AlertTriangle className="h-4 w-4" />
              {publishMsg}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
