import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  Loader2,
  Play,
  RefreshCcw,
  Square,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { scraperApi, type JobSummary } from "@/lib/scraperApi";

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-gray-100 text-gray-700",
  discovering: "bg-blue-100 text-blue-700",
  extracting: "bg-indigo-100 text-indigo-700",
  properties_detected: "bg-yellow-100 text-yellow-800",
  generating: "bg-purple-100 text-purple-700",
  downloading_images: "bg-cyan-100 text-cyan-700",
  completed: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  stopped: "bg-orange-100 text-orange-700",
};

function statusColor(status: string) {
  return STATUS_COLORS[status] ?? "bg-gray-100 text-gray-700";
}

function statusLabel(status: string) {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "short",
    timeStyle: "short",
  });
}

function JobRow({
  job,
  onAction,
}: {
  job: JobSummary;
  onAction: () => void;
}) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      onAction();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="mb-3">
      <CardContent className="p-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium truncate max-w-[200px]" title={job.session_name}>
                {job.session_name || "Unnamed Session"}
              </span>
              <span
                className={`text-xs px-2 py-0.5 rounded-full font-medium ${statusColor(job.status)}`}
              >
                {statusLabel(job.status)}
              </span>
            </div>
            <p className="text-xs text-muted-foreground truncate mt-0.5" title={job.url}>
              {job.url}
            </p>
            <p className="text-xs text-muted-foreground mt-1">{job.progress_msg}</p>
            {job.progress_pct > 0 && job.status !== "completed" && job.status !== "failed" && (
              <Progress value={job.progress_pct} className="mt-2 h-1.5 w-full max-w-xs" />
            )}
          </div>

          <div className="flex flex-wrap items-center gap-1 shrink-0">
            {job.can_open_review && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => navigate("/app/content")}
              >
                Review
              </Button>
            )}
            {job.can_download && (
              <Button
                size="sm"
                variant="outline"
                asChild
              >
                <a href={scraperApi.downloadUrl(job.job_id)} download>
                  <Download className="h-3.5 w-3.5 mr-1" />
                  Download
                </a>
              </Button>
            )}
            {job.can_stop && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => act(() => scraperApi.stop(job.job_id))}
              >
                <Square className="h-3.5 w-3.5 mr-1" />
                Stop
              </Button>
            )}
            {job.can_resume && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => act(() => scraperApi.resume(job.job_id))}
              >
                <Play className="h-3.5 w-3.5 mr-1" />
                Resume
              </Button>
            )}
            {!job.can_stop && (
              <Button
                size="sm"
                variant="ghost"
                className="text-red-500 hover:text-red-700"
                disabled={busy}
                onClick={() => {
                  if (confirm("Delete this session?"))
                    act(() => scraperApi.remove(job.job_id));
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            )}
          </div>
        </div>
        <p className="text-xs text-muted-foreground mt-2">
          Started {formatDate(job.created_at)}
          {job.completed_at ? ` · Finished ${formatDate(job.completed_at)}` : ""}
        </p>
      </CardContent>
    </Card>
  );
}

export default function WebCrawl() {
  const [url, setUrl] = useState("");
  const [projectName, setProjectName] = useState("");
  const [language, setLanguage] = useState("English");
  const [outputMode, setOutputMode] = useState<"both" | "kb_only" | "media_only">("both");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [loadingJobs, setLoadingJobs] = useState(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchJobs = useCallback(async () => {
    try {
      const list = await scraperApi.listJobs();
      setJobs(list);
    } catch {
      // silently ignore poll errors
    } finally {
      setLoadingJobs(false);
    }
  }, []);

  useEffect(() => {
    fetchJobs();
    // Poll faster when jobs are actively running, slower when idle
    const interval = jobs.some((j) => j.can_stop) ? 2000 : 6000;
    pollRef.current = setInterval(fetchJobs, interval);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [fetchJobs, jobs]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setSuccess("");
    if (!url.trim().startsWith("http")) {
      setError("URL must start with http:// or https://");
      return;
    }
    setSubmitting(true);
    try {
      await scraperApi.startScrape({
        url: url.trim(),
        project_name: projectName.trim() || "Hotel Bot",
        language,
        output_mode: outputMode,
      });
      setSuccess("Crawl started. Track progress in the jobs list below.");
      setUrl("");
      setProjectName("");
      await fetchJobs();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start crawl");
    } finally {
      setSubmitting(false);
    }
  }

  const hasActive = jobs.some((j) => j.can_stop);

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-bold">Web Crawling</h1>
        <p className="text-muted-foreground">
          Crawl a hotel website and generate a knowledge base.
        </p>
      </div>

      {/* Start crawl form */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Start New Crawl</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="url">Website URL</Label>
              <Input
                id="url"
                placeholder="https://hotelwebsite.com"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                required
              />
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="project">Project Name</Label>
                <Input
                  id="project"
                  placeholder="Grand Hotel Bot"
                  value={projectName}
                  onChange={(e) => setProjectName(e.target.value)}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="language">Language</Label>
                <Select value={language} onValueChange={setLanguage}>
                  <SelectTrigger id="language">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="English">English</SelectItem>
                    <SelectItem value="Spanish">Spanish</SelectItem>
                    <SelectItem value="French">French</SelectItem>
                    <SelectItem value="German">German</SelectItem>
                    <SelectItem value="Arabic">Arabic</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="output-mode">Output Mode</Label>
              <Select
                value={outputMode}
                onValueChange={(v) => setOutputMode(v as typeof outputMode)}
              >
                <SelectTrigger id="output-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="both">KB + Images</SelectItem>
                  <SelectItem value="kb_only">KB Text Only</SelectItem>
                  <SelectItem value="media_only">Images Only</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {error && (
              <div className="flex items-center gap-2 text-sm text-red-600">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                {error}
              </div>
            )}
            {success && (
              <div className="flex items-center gap-2 text-sm text-green-600">
                <CheckCircle2 className="h-4 w-4 shrink-0" />
                {success}
              </div>
            )}

            <Button type="submit" disabled={submitting}>
              {submitting ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Play className="h-4 w-4 mr-2" />
              )}
              Start Crawl
            </Button>
          </form>
        </CardContent>
      </Card>

      {/* Jobs list */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold">
            Recent Sessions
            {hasActive && (
              <Badge variant="secondary" className="ml-2 text-xs">
                Active
              </Badge>
            )}
          </h2>
          <Button variant="ghost" size="sm" onClick={fetchJobs}>
            <RefreshCcw className="h-3.5 w-3.5" />
          </Button>
        </div>

        {loadingJobs ? (
          <div className="flex items-center gap-2 text-muted-foreground text-sm">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading sessions…
          </div>
        ) : jobs.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No sessions yet. Start a crawl above.
          </p>
        ) : (
          jobs.map((job) => (
            <JobRow key={job.job_id} job={job} onAction={fetchJobs} />
          ))
        )}
      </div>
    </div>
  );
}
