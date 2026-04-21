import { useCallback, useEffect, useState } from "react";
import { Globe, FileText, Bot, Clock, CheckCircle2, AlertCircle, Loader2, RefreshCcw, XCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { scraperApi, type JobSummary } from "@/lib/scraperApi";
import { adminGet } from "@/lib/adminApi";

interface RagStatus {
  status?: string;
  kb_files?: number;
  total_chunks?: number;
}

interface DashboardStats {
  pagesCrawled: number;
  kbFiles: number;
  botStatus: string;
  pendingReview: number;
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function jobToActivity(job: JobSummary): { text: string; time: string; status: "success" | "warning" | "error" } {
  const name = job.session_name || job.url;
  if (job.status === "completed") return { text: `Crawl completed: ${name}`, time: timeAgo(job.completed_at || job.created_at), status: "success" };
  if (job.status === "failed") return { text: `Crawl failed: ${name}`, time: timeAgo(job.completed_at || job.created_at), status: "error" };
  if (job.can_open_review) return { text: `Ready to review: ${name}`, time: timeAgo(job.created_at), status: "warning" };
  return { text: `${job.progress_msg || "Processing"}: ${name}`, time: timeAgo(job.created_at), status: "warning" };
}

const quickLinks = [
  { label: "Start New Crawl", href: "/app/crawl", icon: Globe },
  { label: "Review Content", href: "/app/content", icon: FileText },
  { label: "Configure Bot", href: "/app/training", icon: Bot },
];

export default function Dashboard() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [activity, setActivity] = useState<ReturnType<typeof jobToActivity>[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      // adminGet prepends /admin/api automatically — paths here are relative to that
      const [jobs, rag] = await Promise.allSettled([
        scraperApi.listJobs(),
        adminGet<RagStatus>("/rag/status"),
      ]);

      const jobList: JobSummary[] = jobs.status === "fulfilled" ? jobs.value : [];
      const ragData: RagStatus = rag.status === "fulfilled" ? (rag.value ?? {}) : {};

      const completedJobs = jobList.filter((j) => j.status === "completed").length;

      setStats({
        pagesCrawled: completedJobs,
        kbFiles: ragData.total_chunks ?? ragData.kb_files ?? 0,
        botStatus: ragData.status === "ready" || (ragData.kb_files ?? 0) > 0 ? "Active" : "No KB",
        pendingReview: jobList.filter((j) => j.can_open_review).length,
      });

      setActivity(jobList.slice(0, 5).map(jobToActivity));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const statCards = stats
    ? [
        { label: "Completed Crawls", value: String(stats.pagesCrawled), icon: Globe, color: "text-blue-500" },
        { label: "KB Chunks", value: String(stats.kbFiles || "—"), icon: FileText, color: "text-primary" },
        { label: "Bot Status", value: stats.botStatus, icon: Bot, color: stats.botStatus === "Active" ? "text-green-500" : "text-yellow-500" },
        { label: "Pending Review", value: String(stats.pendingReview), icon: Clock, color: "text-yellow-500" },
      ]
    : [];

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Dashboard</h1>
          <p className="text-muted-foreground">Overview of your Kebo Bot setup</p>
        </div>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
          <RefreshCcw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
        </Button>
      </div>

      {error && (
        <div className="flex items-center gap-2 text-sm text-red-600">
          <AlertCircle className="h-4 w-4" /> {error}
        </div>
      )}

      {/* Stats */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {loading && !stats
          ? Array.from({ length: 4 }).map((_, i) => (
              <Card key={i}>
                <CardContent className="flex h-20 items-center justify-center">
                  <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                </CardContent>
              </Card>
            ))
          : statCards.map((s) => (
              <Card key={s.label}>
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">{s.label}</CardTitle>
                  <s.icon className={`h-4 w-4 ${s.color}`} />
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold">{s.value}</div>
                </CardContent>
              </Card>
            ))}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Recent Activity */}
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Recent Activity</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {loading && activity.length === 0 ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : activity.length === 0 ? (
              <p className="text-sm text-muted-foreground">No crawl sessions yet.</p>
            ) : (
              activity.map((a, i) => {
                const Icon = a.status === "success" ? CheckCircle2 : a.status === "error" ? XCircle : AlertCircle;
                const color = a.status === "success" ? "text-green-500" : a.status === "error" ? "text-red-500" : "text-yellow-500";
                return (
                  <div key={i} className="flex items-start gap-3">
                    <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${color}`} />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm">{a.text}</p>
                      <p className="text-xs text-muted-foreground">{a.time}</p>
                    </div>
                  </div>
                );
              })
            )}
          </CardContent>
        </Card>

        {/* Quick Links */}
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Quick Actions</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {quickLinks.map((l) => (
              <Link
                key={l.label}
                to={l.href}
                className="flex items-center gap-3 rounded-lg border p-3 transition-colors hover:bg-accent"
              >
                <l.icon className="h-5 w-5 text-primary" />
                <span className="text-sm font-medium">{l.label}</span>
              </Link>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
