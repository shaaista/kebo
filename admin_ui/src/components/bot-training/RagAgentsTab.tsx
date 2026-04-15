import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/hooks/use-toast";
import { adminGet, adminSend, normalizePropertyCode, splitLines } from "@/lib/adminApi";
import { Loader2, Play, RefreshCcw, Trash2, Upload } from "lucide-react";

interface RagFileRow {
  stored_name?: string;
  original_name?: string;
  path?: string;
  is_selected?: boolean;
  exists_on_disk?: boolean;
  size_bytes?: number;
  uploaded_at?: string;
}

interface RagJob {
  job_id?: string;
  status?: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
}

interface RagAgentsTabProps {
  propertyCode: string;
  businessType: string;
  businessName?: string;
  city?: string;
}

const RagAgentsTab = ({ propertyCode, businessType, businessName, city }: RagAgentsTabProps) => {
  const { toast } = useToast();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [tenantId, setTenantId] = useState(normalizePropertyCode(propertyCode) || "default");
  const [ragBusinessType, setRagBusinessType] = useState(String(businessType || "generic"));
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [question, setQuestion] = useState("");
  const [clearExisting, setClearExisting] = useState(false);
  const [filePaths, setFilePaths] = useState("");
  const [addToSources, setAddToSources] = useState(true);

  const [statusPayload, setStatusPayload] = useState<Record<string, unknown>>({});
  const [reindexPayload, setReindexPayload] = useState<Record<string, unknown>>({});
  const [uploadPayload, setUploadPayload] = useState<Record<string, unknown>>({});
  const [jobsPayload, setJobsPayload] = useState<Record<string, unknown>>({});
  const [queryPayload, setQueryPayload] = useState<Record<string, unknown>>({});
  const [currentJob, setCurrentJob] = useState<RagJob | null>(null);

  const [ragFiles, setRagFiles] = useState<RagFileRow[]>([]);
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);

  const normalizedTenant = useMemo(
    () => normalizePropertyCode(tenantId) || normalizePropertyCode(propertyCode) || "default",
    [tenantId, propertyCode],
  );

  const clearPollTimer = () => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  useEffect(() => {
    setTenantId(normalizePropertyCode(propertyCode) || "default");
  }, [propertyCode]);

  useEffect(() => {
    setRagBusinessType((prev) => (prev.trim() ? prev : String(businessType || "generic")));
  }, [businessType]);

  useEffect(() => () => clearPollTimer(), []);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await adminGet<Record<string, unknown>>(
        `/rag/status?tenant_id=${encodeURIComponent(normalizedTenant)}`,
        propertyCode,
      );
      setStatusPayload(payload || {});
    } catch (error) {
      toast({
        title: "Failed to load RAG status",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  }, [normalizedTenant, propertyCode, toast]);

  const loadJobs = useCallback(async () => {
    try {
      const payload = await adminGet<Record<string, unknown>>("/rag/jobs?limit=15", propertyCode);
      setJobsPayload(payload || {});
      const jobs = Array.isArray(payload?.jobs) ? (payload.jobs as RagJob[]) : [];
      if (jobs.length > 0) setCurrentJob(jobs[0]);
    } catch {
      setJobsPayload({ error: "Error loading jobs" });
    }
  }, [propertyCode]);

  const loadFiles = useCallback(async () => {
    try {
      const payload = await adminGet<{ files?: RagFileRow[]; selected_sources?: string[] }>(
        `/rag/files?tenant_id=${encodeURIComponent(normalizedTenant)}`,
        propertyCode,
      );
      const files = Array.isArray(payload?.files) ? payload.files : [];
      const selected = Array.isArray(payload?.selected_sources)
        ? payload.selected_sources.map((value) => String(value || "").trim()).filter(Boolean)
        : [];
      setRagFiles(files);
      setSelectedPaths(selected);

      const selectedSet = new Set(selected);
      const selectedUploadedPaths = files
        .map((file) => String(file.path || "").trim())
        .filter((pathValue) => pathValue && selectedSet.has(pathValue));
      setFilePaths(selectedUploadedPaths.join("\n"));
    } catch {
      setRagFiles([]);
      setSelectedPaths([]);
    }
  }, [normalizedTenant, propertyCode]);

  const loadAllRagData = useCallback(async () => {
    await Promise.all([loadStatus(), loadFiles(), loadJobs()]);
  }, [loadStatus, loadFiles, loadJobs]);

  useEffect(() => {
    void loadAllRagData();
  }, [loadAllRagData]);

  const pollJob = useCallback(
    async (jobId: string) => {
      if (!jobId) return;
      clearPollTimer();
      try {
        const job = await adminGet<RagJob>(`/rag/jobs/${encodeURIComponent(jobId)}`, propertyCode);
        setCurrentJob(job || null);
        await loadJobs();
        if (job?.status === "completed") {
          toast({ title: "Background RAG job completed" });
          await loadStatus();
          return;
        }
        if (job?.status === "failed") {
          toast({ title: "Background RAG job failed", variant: "destructive" });
          return;
        }
        pollTimerRef.current = setTimeout(() => void pollJob(jobId), 2000);
      } catch {
        pollTimerRef.current = setTimeout(() => void pollJob(jobId), 3000);
      }
    },
    [loadJobs, loadStatus, propertyCode, toast],
  );

  const runQuery = async () => {
    const message = String(question || "").trim();
    if (!message) {
      toast({ title: "Enter a question", variant: "destructive" });
      return;
    }
    setRunning(true);
    try {
      const payload = await adminSend<Record<string, unknown>>(
        "POST",
        "/rag/query",
        {
          question: message,
          tenant_id: normalizedTenant,
          business_type: String(ragBusinessType || "generic").trim() || "generic",
          hotel_name: String(businessName || "").trim() || "Business",
          city: String(city || "").trim(),
        },
        propertyCode,
      );
      setQueryPayload(payload || {});
    } catch (error) {
      toast({
        title: "RAG query failed",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setRunning(false);
    }
  };

  const runReindex = async () => {
    setRunning(true);
    try {
      const paths = splitLines(filePaths);
      const payload = await adminSend<Record<string, unknown>>(
        "POST",
        "/rag/reindex",
        {
          tenant_id: normalizedTenant,
          business_type: String(ragBusinessType || "generic").trim() || "generic",
          clear_existing: clearExisting,
          ...(paths.length > 0 ? { file_paths: paths } : {}),
        },
        propertyCode,
      );
      setReindexPayload(payload || {});
      toast({ title: "RAG reindex completed" });
      await loadStatus();
    } catch (error) {
      toast({
        title: "RAG reindex failed",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setRunning(false);
    }
  };

  const startAsyncJob = async () => {
    setRunning(true);
    try {
      const job = await adminSend<RagJob>(
        "POST",
        "/rag/jobs/start",
        {
          tenant_id: normalizedTenant,
          business_type: String(ragBusinessType || "generic").trim() || "generic",
          clear_existing: clearExisting,
          file_paths: splitLines(filePaths),
        },
        propertyCode,
      );
      setCurrentJob(job || null);
      toast({ title: `Background RAG job started: ${job?.job_id || "-"}` });
      await loadJobs();
      if (job?.job_id) void pollJob(job.job_id);
    } catch (error) {
      toast({
        title: "Failed to start async RAG job",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setRunning(false);
    }
  };

  const uploadFiles = async () => {
    const files = Array.from(fileInputRef.current?.files || []);
    if (!files.length) {
      toast({ title: "Choose at least one file", variant: "destructive" });
      return;
    }
    setRunning(true);
    try {
      const form = new FormData();
      for (const file of files) form.append("files", file);
      form.append("tenant_id", normalizedTenant);
      form.append("add_to_sources", addToSources ? "true" : "false");

      const payload = await adminSend<Record<string, unknown>>("POST", "/rag/upload", form, propertyCode);
      setUploadPayload(payload || {});
      const uploadedPaths = Array.isArray(payload?.files)
        ? (payload.files as Array<{ path?: string }>).map((row) => String(row.path || "").trim()).filter(Boolean)
        : [];
      if (uploadedPaths.length > 0) {
        const merged = [...new Set([...splitLines(filePaths), ...uploadedPaths])];
        setFilePaths(merged.join("\n"));
      }
      if (fileInputRef.current) fileInputRef.current.value = "";
      toast({ title: `Uploaded ${Number(payload?.uploaded_count || 0)} file(s)` });
      await loadAllRagData();
    } catch (error) {
      toast({
        title: "Upload failed",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setRunning(false);
    }
  };

  const deleteFile = async (storedName: string) => {
    const target = String(storedName || "").trim();
    if (!target) return;
    if (!window.confirm(`Delete KB file "${target}"?`)) return;
    setRunning(true);
    try {
      const payload = await adminSend<Record<string, unknown>>(
        "DELETE",
        `/rag/files/${encodeURIComponent(target)}?tenant_id=${encodeURIComponent(normalizedTenant)}`,
        undefined,
        propertyCode,
      );
      setUploadPayload(payload || {});
      toast({ title: "KB file deleted" });
      await loadAllRagData();
    } catch (error) {
      toast({
        title: "KB delete failed",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setRunning(false);
    }
  };

  const saveSelection = async () => {
    setRunning(true);
    try {
      const config = await adminGet<Record<string, unknown>>("/config/onboarding/knowledge", propertyCode);
      const existingSources = Array.isArray(config?.sources)
        ? config.sources.map((value) => String(value || "").trim()).filter(Boolean)
        : [];
      const uploadedPaths = ragFiles.map((row) => String(row.path || "").trim()).filter(Boolean);
      const selected = selectedPaths.filter(Boolean);
      const preserved = existingSources.filter((source) => !uploadedPaths.includes(source));
      const nextSources = [...new Set([...preserved, ...selected])];

      await adminSend(
        "PUT",
        "/config/onboarding/knowledge",
        {
          sources: nextSources,
          notes: String(config?.notes || ""),
          nlu_policy: {
            dos: Array.isArray((config?.nlu_policy as { dos?: unknown[] } | undefined)?.dos)
              ? ((config?.nlu_policy as { dos?: unknown[] }).dos || [])
              : [],
            donts: Array.isArray((config?.nlu_policy as { donts?: unknown[] } | undefined)?.donts)
              ? ((config?.nlu_policy as { donts?: unknown[] }).donts || [])
              : [],
          },
        },
        propertyCode,
      );
      toast({ title: "RAG file selection saved" });
      await loadFiles();
    } catch (error) {
      toast({
        title: "Failed to save selection",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">RAG Scope</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-3">
          <div className="space-y-1.5">
            <Label>Tenant ID</Label>
            <Input value={tenantId} onChange={(event) => setTenantId(event.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>Business Type</Label>
            <Input value={ragBusinessType} onChange={(event) => setRagBusinessType(event.target.value)} />
          </div>
          <div className="flex items-end">
            <Button variant="outline" onClick={loadAllRagData} disabled={loading || running}>
              {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCcw className="mr-2 h-4 w-4" />}
              Refresh
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">RAG Status</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <Badge>backend: {String(statusPayload.backend_configured || "local")}</Badge>
            <Badge variant={statusPayload.qdrant_ready ? "default" : "destructive"}>
              qdrant ready: {statusPayload.qdrant_ready ? "yes" : "no"}
            </Badge>
            <Badge variant="outline">tenant chunks: {String(statusPayload.tenant_chunks ?? 0)}</Badge>
            <Badge variant="outline">total chunks: {String(statusPayload.local_total_chunks ?? 0)}</Badge>
          </div>
          <pre className="max-h-64 overflow-auto rounded-md border bg-muted/40 p-3 text-xs">
            {JSON.stringify(statusPayload, null, 2)}
          </pre>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">RAG Query Test</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <Textarea
            rows={3}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Ask a question to test retrieval + grounding"
          />
          <Button onClick={runQuery} disabled={running || !question.trim()}>
            {running ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
            Run Query
          </Button>
          <div className="flex flex-wrap gap-2">
            <Badge variant={queryPayload.handled ? "default" : "destructive"}>
              handled: {queryPayload.handled ? "yes" : "no"}
            </Badge>
            <Badge variant="outline">
              confidence:{" "}
              {typeof queryPayload.confidence === "number" ? Number(queryPayload.confidence).toFixed(2) : "-"}
            </Badge>
          </div>
          <Textarea rows={4} value={String(queryPayload.answer || queryPayload.reason || "")} readOnly />
          <Textarea
            rows={3}
            value={Array.isArray(queryPayload.sources) ? (queryPayload.sources as unknown[]).join("\n") : ""}
            readOnly
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">RAG Reindex</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <label className="inline-flex items-center gap-2 text-sm text-muted-foreground">
            <input
              type="checkbox"
              checked={clearExisting}
              onChange={(event) => setClearExisting(event.target.checked)}
            />
            Clear existing vectors before reindex
          </label>
          <Textarea
            rows={4}
            value={filePaths}
            onChange={(event) => setFilePaths(event.target.value)}
            placeholder="Optional local file paths (one per line)"
          />
          <div className="flex flex-wrap gap-2">
            <Button onClick={runReindex} disabled={running}>
              {running ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              Run Reindex
            </Button>
            <Button variant="outline" onClick={startAsyncJob} disabled={running}>
              Start Async Job
            </Button>
          </div>
          <pre className="max-h-64 overflow-auto rounded-md border bg-muted/40 p-3 text-xs">
            {JSON.stringify(reindexPayload, null, 2)}
          </pre>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Upload KB Files</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Input ref={fileInputRef} type="file" multiple className="max-w-lg" />
            <Button onClick={uploadFiles} disabled={running}>
              {running ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Upload className="mr-2 h-4 w-4" />}
              Upload
            </Button>
          </div>
          <label className="inline-flex items-center gap-2 text-sm text-muted-foreground">
            <input type="checkbox" checked={addToSources} onChange={(event) => setAddToSources(event.target.checked)} />
            Add uploaded file paths to knowledge sources automatically
          </label>
          <pre className="max-h-64 overflow-auto rounded-md border bg-muted/40 p-3 text-xs">
            {JSON.stringify(uploadPayload, null, 2)}
          </pre>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Uploaded Files</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {ragFiles.length === 0 ? (
            <p className="text-sm text-muted-foreground">No uploaded KB files for this tenant.</p>
          ) : (
            ragFiles.map((file) => {
              const stored = String(file.stored_name || "").trim();
              const pathValue = String(file.path || "").trim();
              const checked = selectedPaths.includes(pathValue);
              return (
                <div key={stored || pathValue} className="rounded-md border p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p className="font-medium">{file.original_name || file.stored_name || "KB file"}</p>
                      <p className="text-xs text-muted-foreground">{pathValue || "-"}</p>
                      <p className="text-xs text-muted-foreground">
                        size: {Number(file.size_bytes || 0)} bytes | disk: {file.exists_on_disk ? "yes" : "no"}
                      </p>
                    </div>
                    {stored ? (
                      <Button variant="destructive" size="sm" onClick={() => void deleteFile(stored)} disabled={running}>
                        <Trash2 className="mr-2 h-4 w-4" /> Delete
                      </Button>
                    ) : null}
                  </div>
                  <label className="mt-2 inline-flex items-center gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(event) => {
                        setSelectedPaths((prev) => {
                          if (!pathValue) return prev;
                          if (event.target.checked) return [...new Set([...prev, pathValue])];
                          return prev.filter((value) => value !== pathValue);
                        });
                      }}
                    />
                    Use as selected knowledge source
                  </label>
                </div>
              );
            })
          )}
          <Button variant="outline" onClick={saveSelection} disabled={running}>
            Save Selection
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Background Jobs</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <Badge>job: {String(currentJob?.job_id || "-")}</Badge>
            <Badge variant={currentJob?.status === "failed" ? "destructive" : "outline"}>
              status: {String(currentJob?.status || "-")}
            </Badge>
            <Badge variant="outline">
              updated: {String(currentJob?.finished_at || currentJob?.started_at || currentJob?.created_at || "-")}
            </Badge>
          </div>
          <pre className="max-h-64 overflow-auto rounded-md border bg-muted/40 p-3 text-xs">
            {JSON.stringify(jobsPayload, null, 2)}
          </pre>
        </CardContent>
      </Card>
    </div>
  );
};

export default RagAgentsTab;
