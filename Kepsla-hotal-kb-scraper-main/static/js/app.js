
const { useEffect, useMemo, useRef, useState } = React;

const STATUS_LABELS = {
  pending: "Queued",
  discovering: "Discovering",
  crawling: "Crawling",
  properties_detected: "Ready for Review",
  extracting: "Publishing",
  generating: "Generating",
  downloading_images: "Downloading Images",
  stopped: "Stopped",
  completed: "Completed",
  failed: "Failed",
};

const DEFAULT_DOWNLOADS = { visible: false, href: "#", note: "" };

const toneFor = (status) => {
  if (status === "completed") return "success";
  if (status === "failed") return "failed";
  if (status === "properties_detected" || status === "stopped") return "review";
  if (status === "pending") return "pending";
  return "progress";
};

const queueLabel = (queueState, taskType) => {
  if (queueState === "running") return taskType === "publish" ? "Publishing" : "Running";
  if (queueState === "queued") return taskType === "publish" ? "Publish Queue" : "Queue";
  if (queueState === "retry_wait") return "Retry Wait";
  if (queueState === "stopped") return "Stopped";
  if (taskType === "publish" && queueState === "idle") return "Publish Ready";
  return "Idle";
};

const isHttpUrl = (value) => {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
};

const deepClone = (value) => JSON.parse(JSON.stringify(value));

const outputLocation = (pathValue) => {
  if (!pathValue) return "";
  return pathValue.replace(/[\\/][^\\/]+\.zip$/i, "");
};

const buildStatusMeta = (statusData, jobId) => {
  const parts = [];
  if (jobId) parts.push(`Job ${String(jobId).slice(0, 8)}`);
  if (typeof statusData.progress_pct === "number") parts.push(`${statusData.progress_pct}%`);
  if (statusData.pages_found) parts.push(`${statusData.pages_found} pages found`);
  if (statusData.properties_found) parts.push(`${statusData.properties_found} entities`);
  return parts.join(" | ");
};

const buildItemMeta = (item) => {
  if (!item) return "";
  if (item.type === "page") return item.meta_text || item.display_path || "";
  const values = [];
  if (item.meta_text && item.meta_text !== item.name && item.meta_text !== item.suggested_name) {
    values.push(item.meta_text);
  }
  if (item.display_path) values.push(item.display_path);
  return values.join(" | ");
};

const resolveInitialScreen = () => {
  try {
    const requested = new URLSearchParams(window.location.search).get("screen");
    return requested === "review" ? "review" : "crawl";
  } catch {
    return "crawl";
  }
};

const formatDateTime = (isoValue) => {
  if (!isoValue) return "";
  const parsed = new Date(isoValue);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleString();
};

const iconForItemType = (itemType) => {
  if (itemType === "image") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <rect x="3.5" y="4.5" width="17" height="15" rx="2.5"></rect>
        <circle cx="9" cy="10" r="1.5"></circle>
        <path d="m20.5 15-4.5-4.5L7 20"></path>
      </svg>
    );
  }
  if (itemType === "video") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <rect x="3.5" y="5.5" width="13" height="13" rx="2.5"></rect>
        <path d="m11 9.5 4 3-4 3z"></path>
        <path d="M16.5 10.5 21 8v9l-4.5-2.5"></path>
      </svg>
    );
  }
  if (itemType === "file") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M7.5 3.5h7l4 4v13h-11z"></path>
        <path d="M14.5 3.5v4h4"></path>
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4.5 5.5h15"></path>
      <path d="M4.5 12h15"></path>
      <path d="M4.5 18.5h10"></path>
    </svg>
  );
};

function normaliseReview(review) {
  if (!review || !Array.isArray(review.entities)) return review;
  review.entities.forEach((entity) => {
    entity.enabled = entity.enabled !== false;
    if (!Array.isArray(entity.items)) entity.items = [];
    entity.items.forEach((item) => {
      item.enabled = item.enabled !== false;
    });
    entity.total_count = entity.items.length;
    entity.enabled_count = entity.items.filter((item) => item.enabled).length;
  });
  return review;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || payload.message || `Request failed with ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function StatusPanel({ id, panel }) {
  if (!panel) return <div id={id} className="status-panel is-hidden" />;
  const hasProgress = Number.isFinite(panel.progressPct);
  const pct = hasProgress ? Math.max(0, Math.min(100, Math.round(Number(panel.progressPct)))) : null;
  return (
    <div id={id} className="status-panel">
      <div className="status-copy">
        <div className="status-main">
          <span className={`status-badge is-${panel.tone}`}>{panel.label}</span>
          <span>{panel.message}</span>
        </div>
        {hasProgress ? (
          <div className="status-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={pct}>
            <div className={`status-progress-fill is-${panel.tone}`} style={{ width: `${pct}%` }} />
          </div>
        ) : null}
      </div>
      {panel.meta ? <div className="status-meta">{panel.meta}</div> : null}
    </div>
  );
}

function App() {
  const [screen, setScreen] = useState(resolveInitialScreen);
  const [theme, setTheme] = useState(localStorage.getItem("hotel-kb-theme") || "light");
  const [jobs, setJobs] = useState([]);
  const [sessionError, setSessionError] = useState("");
  const [currentJobId, setCurrentJobId] = useState(null);
  const [reviewData, setReviewData] = useState(null);
  const [reviewSearch, setReviewSearch] = useState("");
  const [expandedEntities, setExpandedEntities] = useState(new Set());
  const [reviewLoading, setReviewLoading] = useState(false);
  const [crawlBusy, setCrawlBusy] = useState(false);
  const [publishMode, setPublishMode] = useState("idle");
  const [crawlStatus, setCrawlStatus] = useState(null);
  const [publishStatus, setPublishStatus] = useState(null);
  const [downloads, setDownloads] = useState(DEFAULT_DOWNLOADS);
  const [jobOutputDir, setJobOutputDir] = useState("");
  const [accordion, setAccordion] = useState({ project: true, crawlSettings: false });

  const [projectName, setProjectName] = useState("Grand Hotel Bot");
  const [botEnabled, setBotEnabled] = useState(true);
  const [language, setLanguage] = useState("English");
  const [autoSync, setAutoSync] = useState(false);
  const [urlInput, setUrlInput] = useState("https://www.khil.com");
  const [specificUrlInput, setSpecificUrlInput] = useState("");
  const [specificUrls, setSpecificUrls] = useState([]);

  const [editor, setEditor] = useState(null);
  const [editorName, setEditorName] = useState("");
  const [editorLabel, setEditorLabel] = useState("");

  const pollRef = useRef(null);
  const jobsPollRef = useRef(null);
  const currentJobIdRef = useRef(currentJobId);
  const reviewRef = useRef(reviewData);
  const reviewLoadingRef = useRef(reviewLoading);
  const outputDirRef = useRef(jobOutputDir);

  useEffect(() => {
    currentJobIdRef.current = currentJobId;
  }, [currentJobId]);

  useEffect(() => {
    reviewRef.current = reviewData;
  }, [reviewData]);

  useEffect(() => {
    reviewLoadingRef.current = reviewLoading;
  }, [reviewLoading]);

  useEffect(() => {
    outputDirRef.current = jobOutputDir;
  }, [jobOutputDir]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("hotel-kb-theme", theme);
  }, [theme]);

  useEffect(() => {
    document.body.classList.toggle("modal-open", Boolean(editor));
    return () => {
      document.body.classList.remove("modal-open");
    };
  }, [editor]);

  useEffect(() => {
    if (!editor) return undefined;
    const handler = (event) => {
      if (event.key === "Escape") closeEditor();
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
    };
  }, [editor]);

  useEffect(() => {
    loadJobs();
    jobsPollRef.current = window.setInterval(() => loadJobs({ silent: true }), 5000);
    return () => {
      if (jobsPollRef.current) window.clearInterval(jobsPollRef.current);
      stopPolling();
    };
  }, []);

  const currentJob = useMemo(
    () => jobs.find((job) => job.job_id === currentJobId) || null,
    [jobs, currentJobId]
  );

  const filteredEntities = useMemo(() => {
    if (!reviewData || !Array.isArray(reviewData.entities)) return [];
    const query = reviewSearch.trim().toLowerCase();
    return reviewData.entities.reduce((acc, entity, index) => {
      const entityMatch = !query || (entity.name || entity.suggested_name || "").toLowerCase().includes(query);
      const matchingItems = (entity.items || []).filter((item) => {
        if (entityMatch) return true;
        const haystack = [item.name, item.suggested_name, item.label, item.suggested_label, item.display_path, item.meta_text, item.url, item.source_page]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        return haystack.includes(query);
      });
      if (!entityMatch && matchingItems.length === 0) return acc;
      acc.push({ entity, index, items: matchingItems, forceOpen: Boolean(query) });
      return acc;
    }, []);
  }, [reviewData, reviewSearch]);

  const hasSelectedEntities = Boolean(reviewData?.entities?.some((entity) => entity.enabled !== false));
  const publishText = publishMode === "busy" ? "Publishing..." : publishMode === "completed" ? "Published" : "Publish Selected to Knowledge Base";
  const publishDisabled = publishMode === "busy" || publishMode === "completed" || !hasSelectedEntities || !currentJobId;

  function stopPolling() {
    if (!pollRef.current) return;
    window.clearInterval(pollRef.current);
    pollRef.current = null;
  }

  function startPolling(jobId) {
    stopPolling();
    pollStatus(jobId);
    pollRef.current = window.setInterval(() => pollStatus(jobId), 2000);
  }

  function setStatusFromData(statusData, setter) {
    setter({
      tone: toneFor(statusData.status),
      label: STATUS_LABELS[statusData.status] || "Queued",
      message: statusData.error_message || statusData.progress_msg || "Working...",
      meta: buildStatusMeta(statusData, currentJobIdRef.current || statusData.job_id),
      progressPct: statusData.progress_pct,
    });
  }

  function updateReviewDraft(mutator) {
    setReviewData((prev) => {
      if (!prev) return prev;
      const next = deepClone(prev);
      mutator(next);
      return normaliseReview(next);
    });
  }
  async function loadJobs({ silent = false } = {}) {
    try {
      const payload = await fetchJson("/api/jobs");
      setJobs(Array.isArray(payload) ? payload : []);
      if (!silent) setSessionError("");
    } catch (error) {
      if (!silent) setSessionError(error.message || "Could not load sessions right now.");
    }
  }

  async function handleMissingSession(jobId, message) {
    stopPolling();
    setJobs((prev) => prev.filter((job) => job.job_id !== jobId));
    if (currentJobIdRef.current === jobId) {
      setCurrentJobId(null);
      setReviewData(null);
      setScreen("crawl");
      setDownloads(DEFAULT_DOWNLOADS);
      setPublishStatus(null);
      setPublishMode("idle");
    }
    setCrawlBusy(false);
    setCrawlStatus({ tone: "failed", label: "Missing", message, meta: "Refresh sessions and retry." });
    await loadJobs({ silent: true });
  }

  async function loadReview(jobId) {
    if (reviewLoadingRef.current) return;
    setReviewLoading(true);
    try {
      const response = await fetchJson(`/api/review/${jobId}`);
      const payload = normaliseReview(deepClone(response.review_data));
      setReviewData(payload);
      setCurrentJobId(response.job_id);
      setScreen("review");
      setPublishStatus(null);
      setReviewSearch("");

      const project = payload.project || {};
      setProjectName(project.name || "Grand Hotel Bot");
      setBotEnabled(Boolean(project.bot_enabled));
      setLanguage(project.language || "English");
      setAutoSync(Boolean(project.auto_sync));
      setUrlInput(payload.source_url || "");
      setSpecificUrls(Array.isArray(project.specific_urls) ? [...project.specific_urls] : []);

      if (payload.entities.length > 0) {
        setExpandedEntities((prev) => (prev.size > 0 ? prev : new Set([payload.entities[0].id])));
      }

      if (response.status === "completed") {
        setPublishMode("completed");
        const outDir = outputDirRef.current || "";
        const location = outputLocation(outDir);
        setDownloads({
          visible: true,
          href: `/api/download/${jobId}`,
          note: location ? `Files are saved under ${location}.` : "Download ZIP contains all files.",
        });
      } else {
        setPublishMode("ready");
        setDownloads(DEFAULT_DOWNLOADS);
      }
    } catch (error) {
      if (error.status === 404) {
        await handleMissingSession(jobId, "Review data for this session is unavailable.");
      } else {
        setCrawlStatus({ tone: "failed", label: "Failed", message: error.message || "Could not load review data.", meta: "" });
      }
    } finally {
      setReviewLoading(false);
    }
  }

  async function applyStatusToUi(statusData) {
    if (statusData.job_id !== currentJobIdRef.current) return;

    setJobOutputDir(statusData.output_dir || "");
    if (reviewRef.current) setStatusFromData(statusData, setPublishStatus);
    else setStatusFromData(statusData, setCrawlStatus);

    if (!reviewRef.current && statusData.can_open_review) {
      await loadReview(statusData.job_id);
    }

    if (["discovering", "crawling", "extracting", "generating", "downloading_images"].includes(statusData.status)) {
      setCrawlBusy(true);
      if (["extracting", "generating", "downloading_images"].includes(statusData.status)) {
        setPublishMode("busy");
      }
    }

    if (statusData.status === "properties_detected" || statusData.status === "stopped") {
      setCrawlBusy(false);
      setPublishMode("ready");
      stopPolling();
    }

    if (statusData.status === "completed") {
      stopPolling();
      setCrawlBusy(false);
      setPublishMode("completed");
      const outDir = statusData.output_dir || outputDirRef.current;
      const location = outputLocation(outDir);
      setDownloads({
        visible: true,
        href: `/api/download/${statusData.job_id}`,
        note: location ? `Files are saved under ${location}.` : "Download ZIP contains all files.",
      });
    }

    if (statusData.status === "failed") {
      stopPolling();
      setCrawlBusy(false);
      if (reviewRef.current) setPublishMode("ready");
    }
  }

  async function handleStatusUpdate(statusData) {
    setJobs((prev) => {
      const idx = prev.findIndex((job) => job.job_id === statusData.job_id);
      if (idx === -1) return [statusData, ...prev];
      const next = [...prev];
      next[idx] = { ...next[idx], ...statusData };
      return next;
    });
    await applyStatusToUi(statusData);
  }

  async function pollStatus(jobId) {
    try {
      const statusData = await fetchJson(`/api/status/${jobId}`);
      await handleStatusUpdate(statusData);
    } catch (error) {
      if (error.status === 404) {
        await handleMissingSession(jobId, "This session no longer exists.");
      } else {
        setCrawlStatus({ tone: "failed", label: "Failed", message: error.message || "Could not read session status.", meta: "" });
      }
    }
  }

  async function startCrawl() {
    const targetUrl = (urlInput || "").trim();
    if (!isHttpUrl(targetUrl)) {
      setCrawlStatus({ tone: "failed", label: "Invalid URL", message: "Enter a valid http/https URL.", meta: "" });
      return;
    }

    stopPolling();
    setCurrentJobId(null);
    setReviewData(null);
    setScreen("crawl");
    setDownloads(DEFAULT_DOWNLOADS);
    setPublishStatus(null);
    setPublishMode("idle");
    setCrawlBusy(true);
    setCrawlStatus({ tone: "pending", label: "Queued", message: "Submitting crawl request...", meta: "" });

    try {
      const payload = {
        url: targetUrl,
        project_name: (projectName || "").trim() || "Grand Hotel Bot",
        bot_enabled: Boolean(botEnabled),
        language: (language || "English").trim() || "English",
        specific_urls: [...specificUrls],
        auto_sync: Boolean(autoSync),
      };
      const response = await fetchJson("/api/scrape", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      setCurrentJobId(response.job_id);
      setStatusFromData({
        job_id: response.job_id,
        status: response.status,
        progress_msg: response.message,
        progress_pct: 0,
        pages_found: 0,
        properties_found: 0,
      }, setCrawlStatus);
      await loadJobs({ silent: true });
      startPolling(response.job_id);
    } catch (error) {
      setCrawlBusy(false);
      setCrawlStatus({ tone: "failed", label: "Failed", message: error.message || "Could not start crawl.", meta: "" });
    }
  }

  async function openSession(job) {
    setCurrentJobId(job.job_id);
    setJobOutputDir(job.output_dir || "");
    setCrawlStatus({
      tone: toneFor(job.status),
      label: STATUS_LABELS[job.status] || "Queued",
      message: job.progress_msg || "Ready",
      meta: buildStatusMeta(job, job.job_id),
      progressPct: job.progress_pct,
    });

    if (job.can_open_review) {
      await loadReview(job.job_id);
      if (!["completed", "failed", "properties_detected", "stopped"].includes(job.status)) {
        startPolling(job.job_id);
      }
      return;
    }

    setScreen("crawl");
    setReviewData(null);
    setPublishStatus(null);
    setPublishMode("idle");
    setDownloads(DEFAULT_DOWNLOADS);
    startPolling(job.job_id);
  }

  async function stopSession(jobId) {
    try {
      const response = await fetchJson(`/api/jobs/${jobId}/stop`, { method: "POST" });
      await loadJobs({ silent: true });
      if (jobId === currentJobIdRef.current) {
        setCrawlBusy(false);
        setStatusFromData({
          job_id: jobId,
          status: "stopped",
          progress_msg: response.message || "Session stopped.",
          progress_pct: currentJob?.progress_pct || 0,
          pages_found: currentJob?.pages_found || 0,
          properties_found: currentJob?.properties_found || 0,
        }, reviewRef.current ? setPublishStatus : setCrawlStatus);
      }
      stopPolling();
    } catch (error) {
      setCrawlStatus({ tone: "failed", label: "Failed", message: error.message || "Could not stop session.", meta: "" });
    }
  }

  async function resumeSession(jobId) {
    try {
      const response = await fetchJson(`/api/jobs/${jobId}/resume`, { method: "POST" });
      await loadJobs({ silent: true });
      if (jobId === currentJobIdRef.current) {
        setCrawlBusy(true);
        setStatusFromData({
          job_id: jobId,
          status: "pending",
          progress_msg: response.message || "Session queued to resume.",
          progress_pct: currentJob?.progress_pct || 0,
          pages_found: currentJob?.pages_found || 0,
          properties_found: currentJob?.properties_found || 0,
        }, reviewRef.current ? setPublishStatus : setCrawlStatus);
      }
      startPolling(jobId);
    } catch (error) {
      setCrawlStatus({ tone: "failed", label: "Failed", message: error.message || "Could not resume session.", meta: "" });
    }
  }

  async function deleteSession(jobId) {
    const confirmed = window.confirm("Delete this session and its generated files?");
    if (!confirmed) return;

    try {
      await fetchJson(`/api/jobs/${jobId}`, { method: "DELETE" });
      setJobs((prev) => prev.filter((job) => job.job_id !== jobId));

      if (jobId === currentJobIdRef.current) {
        stopPolling();
        setCurrentJobId(null);
        setReviewData(null);
        setScreen("crawl");
        setDownloads(DEFAULT_DOWNLOADS);
        setPublishStatus(null);
        setPublishMode("idle");
        setJobOutputDir("");
        setCrawlBusy(false);
        setCrawlStatus({ tone: "pending", label: "Deleted", message: "Session deleted.", meta: "" });
      }

      await loadJobs({ silent: true });
    } catch (error) {
      setCrawlStatus({ tone: "failed", label: "Failed", message: error.message || "Could not delete session.", meta: "" });
    }
  }

  async function publishSelected() {
    if (!currentJobId || !reviewData) return;

    const reviewPayload = deepClone(reviewData);
    reviewPayload.project = {
      ...(reviewPayload.project || {}),
      name: (projectName || "").trim() || "Grand Hotel Bot",
      bot_enabled: Boolean(botEnabled),
      language: (language || "English").trim() || "English",
      auto_sync: Boolean(autoSync),
      specific_urls: [...specificUrls],
    };
    normaliseReview(reviewPayload);

    setPublishMode("busy");
    setPublishStatus({
      tone: "progress",
      label: "Publishing",
      message: "Queueing reviewed content for publishing...",
      meta: `Job ${String(currentJobId).slice(0, 8)}`,
      progressPct: 76,
    });

    try {
      const response = await fetchJson(`/api/publish/${currentJobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ review_data: reviewPayload }),
      });
      setReviewData(reviewPayload);
      setPublishStatus((prev) => ({ ...(prev || {}), message: response.message || "Publishing queued." }));
      await loadJobs({ silent: true });
      startPolling(currentJobId);
    } catch (error) {
      setPublishMode("ready");
      setPublishStatus({ tone: "failed", label: "Failed", message: error.message || "Could not publish selected content.", meta: "" });
    }
  }

  function toggleAccordion(key) {
    setAccordion((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  function addSpecificUrl() {
    const trimmed = (specificUrlInput || "").trim();
    if (!trimmed) return;
    if (!isHttpUrl(trimmed)) {
      setCrawlStatus({ tone: "failed", label: "Invalid URL", message: "Specific URL must start with http:// or https://", meta: "" });
      return;
    }
    setSpecificUrls((prev) => (prev.includes(trimmed) ? prev : [...prev, trimmed]));
    setSpecificUrlInput("");
  }

  function removeSpecificUrl(target) {
    setSpecificUrls((prev) => prev.filter((value) => value !== target));
  }

  function toggleEntityOpen(entityId) {
    setExpandedEntities((prev) => {
      const next = new Set(prev);
      if (next.has(entityId)) next.delete(entityId);
      else next.add(entityId);
      return next;
    });
  }

  function toggleEntityEnabled(entityId) {
    updateReviewDraft((next) => {
      const entity = next.entities.find((entry) => entry.id === entityId);
      if (!entity) return;
      entity.enabled = !(entity.enabled !== false);
    });
  }

  function toggleItemEnabled(entityId, itemId) {
    updateReviewDraft((next) => {
      const entity = next.entities.find((entry) => entry.id === entityId);
      if (!entity) return;
      const item = entity.items.find((entry) => entry.id === itemId);
      if (!item) return;
      item.enabled = !(item.enabled !== false);
    });
  }

  function openEditor(entityId, itemId) {
    if (!reviewData) return;
    const entity = reviewData.entities.find((entry) => entry.id === entityId);
    if (!entity) return;
    const item = entity.items.find((entry) => entry.id === itemId);
    if (!item) return;
    setEditor({
      entityId,
      itemId,
      itemType: item.type || "page",
      previewUrl: item.preview_url || "",
      suggestedName: item.suggested_name || "",
      suggestedLabel: item.suggested_label || "",
      sourcePath: item.display_path || item.url || "",
    });
    setEditorName(item.name || item.suggested_name || "");
    setEditorLabel(item.label || item.suggested_label || "");
  }

  function closeEditor() {
    setEditor(null);
    setEditorName("");
    setEditorLabel("");
  }

  function resetEditorToSuggested() {
    if (!editor) return;
    setEditorName(editor.suggestedName || "");
    setEditorLabel(editor.suggestedLabel || "");
  }

  function saveEditor() {
    if (!editor) return;
    updateReviewDraft((next) => {
      const entity = next.entities.find((entry) => entry.id === editor.entityId);
      if (!entity) return;
      const item = entity.items.find((entry) => entry.id === editor.itemId);
      if (!item) return;
      const name = (editorName || "").trim();
      const label = (editorLabel || "").trim();
      item.name = name || item.suggested_name || item.name;
      item.label = label || item.suggested_label || item.label;
    });
    closeEditor();
  }

  return (
    <>
      <header className="topbar">
        <button id="nav-home" className="icon-button" type="button" aria-label="Dashboard" onClick={() => setScreen("crawl")}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="3.5" y="4.5" width="17" height="15" rx="2.5"></rect>
            <path d="M11.5 4.5v15"></path>
          </svg>
        </button>
        <button id="theme-toggle" className="icon-button" type="button" aria-label="Theme" onClick={() => setTheme((prev) => (prev === "dark" ? "light" : "dark"))}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M20 14.5A8.5 8.5 0 1 1 9.5 4 7 7 0 0 0 20 14.5Z"></path>
          </svg>
        </button>
      </header>

      <main className="app-shell">
        <section id="crawl-screen" className={`screen ${screen === "crawl" ? "is-active" : ""}`}>
          <div className="screen-header">
            <h1>Web Crawling</h1>
            <p>Crawl your website to extract content for the knowledge base</p>
          </div>

          <section className="card sessions-panel">
            <div className="sessions-header">
              <div>
                <h2>Sessions</h2>
                <p>Reopen, stop, resume, download, or remove previous crawl sessions.</p>
              </div>
              <button id="sessions-refresh" className="ghost-button primary-button-compact" type="button" onClick={() => loadJobs()}>
                Refresh Sessions
              </button>
            </div>

            <div id="session-list" className="session-list">
              {sessionError ? <div className="empty-state">{sessionError}</div> : null}
              {!sessionError && jobs.length === 0 ? <div className="empty-state">No sessions yet. Start a crawl to create one.</div> : null}

              {jobs.map((job) => {
                const isCurrent = job.job_id === currentJobId;
                const queueText = queueLabel(job.queue_state, job.task_type);
                const canReview = Boolean(job.can_open_review);
                const canStop = Boolean(job.can_stop);
                const canResume = Boolean(job.can_resume);
                const canDownload = Boolean(job.can_download);
                const canDelete = !canStop && !canResume && ["completed", "failed", "properties_detected", "stopped"].includes(job.status);
                return (
                  <article key={job.job_id} className={`session-card ${isCurrent ? "is-current" : ""}`}>
                    <div className="session-card-main">
                      <div className="session-copy">
                        <div className="session-topline">
                          {isCurrent ? <span className="session-current-pill">Current</span> : null}
                          <span className="session-queue-pill">{queueText}</span>
                          <span className={`status-badge is-${toneFor(job.status)}`}>{STATUS_LABELS[job.status] || "Queued"}</span>
                        </div>
                        <h3 className="session-title">{job.session_name || "Session"}</h3>
                        <p className="session-url">{job.url || "-"}</p>
                        <p className="session-meta">
                          Created {formatDateTime(job.created_at)}
                          {job.completed_at ? ` | Completed ${formatDateTime(job.completed_at)}` : ""}
                        </p>
                        <p className="session-message">{job.progress_msg || "No progress message available."}</p>
                      </div>

                      <div className="session-actions">
                        <button className="ghost-button" type="button" onClick={() => openSession(job)}>
                          {canReview ? "Open Review" : "Open Session"}
                        </button>

                        {canStop ? (
                          <button className="ghost-button" type="button" onClick={() => stopSession(job.job_id)}>
                            Stop
                          </button>
                        ) : null}

                        {canResume ? (
                          <button className="ghost-button" type="button" onClick={() => resumeSession(job.job_id)}>
                            Resume
                          </button>
                        ) : null}

                        {canDownload ? (
                          <a className="primary-button" href={`/api/download/${job.job_id}`} onClick={(event) => event.stopPropagation()}>
                            Download
                          </a>
                        ) : null}

                        {canDelete ? (
                          <button className="ghost-button danger-button" type="button" onClick={() => deleteSession(job.job_id)}>
                            Remove
                          </button>
                        ) : null}
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>

          <section className={`card accordion ${accordion.project ? "is-open" : ""}`} data-accordion="project">
            <button className="accordion-toggle" type="button" aria-expanded={accordion.project} onClick={() => toggleAccordion("project")}>
              <span className="accordion-title">
                <span className="section-icon">
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M12 3v10"></path>
                    <path d="M8 7l4-4 4 4"></path>
                    <path d="M5 13a7 7 0 1 0 14 0"></path>
                  </svg>
                </span>
                <span>Project Settings</span>
              </span>
              <span className="accordion-chevron">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="m7 14 5-5 5 5"></path>
                </svg>
              </span>
            </button>
            <div className="accordion-body">
              <div>
                <div className="field-group">
                  <label className="field-label" htmlFor="project-name">Project Name</label>
                  <input id="project-name" className="text-input" type="text" value={projectName} onChange={(event) => setProjectName(event.target.value)} placeholder="Grand Hotel Bot" />
                </div>

                <div className="toggle-row">
                  <div>
                    <p className="field-label">Bot Enabled</p>
                    <p className="field-help">Toggle the bot on/off on your website</p>
                  </div>
                  <label className="switch" htmlFor="bot-enabled">
                    <input id="bot-enabled" type="checkbox" checked={botEnabled} onChange={(event) => setBotEnabled(event.target.checked)} />
                    <span className="switch-ui"></span>
                  </label>
                </div>

                <div className="field-group">
                  <label className="field-label" htmlFor="language-select">Language</label>
                  <div className="select-wrap">
                    <select id="language-select" className="text-input text-select" value={language} onChange={(event) => setLanguage(event.target.value)}>
                      <option value="English">English</option>
                      <option value="Hindi">Hindi</option>
                      <option value="Spanish">Spanish</option>
                      <option value="French">French</option>
                    </select>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section className={`card accordion ${accordion.crawlSettings ? "is-open" : ""}`} data-accordion="crawl-settings">
            <button className="accordion-toggle" type="button" aria-expanded={accordion.crawlSettings} onClick={() => toggleAccordion("crawlSettings")}>
              <span className="accordion-title">
                <span className="section-icon">
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M4 7h8"></path>
                    <path d="M4 17h16"></path>
                    <path d="M14 7h6"></path>
                    <path d="M10 17h4"></path>
                    <circle cx="12" cy="7" r="2.25"></circle>
                    <circle cx="8" cy="17" r="2.25"></circle>
                  </svg>
                </span>
                <span>Crawl Settings</span>
              </span>
              <span className="accordion-chevron">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="m7 10 5 5 5-5"></path>
                </svg>
              </span>
            </button>
            <div className="accordion-body">
              <div>
                <div className="field-group">
                  <label className="field-label" htmlFor="specific-url-input">Specific URLs</label>
                  <p className="field-help">Add specific pages you want the bot to crawl</p>
                  <div className="inline-input-row">
                    <input
                      id="specific-url-input"
                      className="text-input"
                      type="url"
                      value={specificUrlInput}
                      onChange={(event) => setSpecificUrlInput(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          addSpecificUrl();
                        }
                      }}
                      placeholder="https://example.com/page"
                    />
                    <button id="add-specific-url" className="ghost-button" type="button" onClick={addSpecificUrl}>
                      Add URL
                    </button>
                  </div>

                  <div id="specific-url-list" className="chip-list">
                    {specificUrls.map((entry) => (
                      <span key={entry} className="chip">
                        <span>{entry}</span>
                        <button type="button" aria-label={`Remove ${entry}`} onClick={() => removeSpecificUrl(entry)}>
                          <svg viewBox="0 0 24 24" aria-hidden="true">
                            <path d="m6 6 12 12"></path>
                            <path d="M18 6 6 18"></path>
                          </svg>
                        </button>
                      </span>
                    ))}
                  </div>
                </div>

                <div className="toggle-row">
                  <div>
                    <p className="field-label">Auto-sync</p>
                    <p className="field-help">Automatically re-crawl and update knowledge base</p>
                  </div>
                  <label className="switch" htmlFor="auto-sync">
                    <input id="auto-sync" type="checkbox" checked={autoSync} onChange={(event) => setAutoSync(event.target.checked)} />
                    <span className="switch-ui switch-ui-muted"></span>
                  </label>
                </div>
              </div>
            </div>
          </section>

          <section className="card start-card">
            <div className="start-card-copy">
              <h2>Start a Crawl</h2>
              <p>Enter your website URL to begin crawling</p>
            </div>

            <div className="start-form-row">
              <div className="url-input-wrap">
                <span className="url-input-icon">
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <circle cx="12" cy="12" r="8.5"></circle>
                    <path d="M3.5 12h17"></path>
                    <path d="M12 3.5a13.5 13.5 0 0 1 0 17"></path>
                    <path d="M12 3.5a13.5 13.5 0 0 0 0 17"></path>
                  </svg>
                </span>
                <input
                  id="url-input"
                  className="text-input url-input"
                  type="url"
                  value={urlInput}
                  onChange={(event) => setUrlInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      startCrawl();
                    }
                  }}
                  placeholder="https://www.khil.com"
                  autoComplete="url"
                  spellCheck="false"
                />
              </div>

              <button id="crawl-button" className="primary-button" type="button" onClick={startCrawl} disabled={crawlBusy}>
                <span className="button-icon">
                  <svg viewBox="0 0 24 24" aria-hidden="true" className={crawlBusy ? "spin" : ""}>
                    <path d="M7 5.5 18 12 7 18.5Z"></path>
                  </svg>
                </span>
                <span id="crawl-button-text">{crawlBusy ? "Working..." : "Start Crawl"}</span>
              </button>
            </div>

            <StatusPanel id="crawl-status" panel={crawlStatus} />
          </section>
        </section>

        <section id="review-screen" className={`screen ${screen === "review" ? "is-active" : ""}`}>
          <div className="manager-header">
            <div>
              <h1>Content Manager</h1>
              <p>Review, label, and enable/disable crawled content before training</p>
            </div>
            <button id="publish-button" className="primary-button primary-button-compact" type="button" onClick={publishSelected} disabled={publishDisabled}>
              {publishText}
            </button>
          </div>

          <StatusPanel id="publish-status" panel={publishStatus} />

          <div className="search-wrap">
            <span className="url-input-icon">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="11" cy="11" r="6.5"></circle>
                <path d="m16 16 4.25 4.25"></path>
              </svg>
            </span>
            <input id="review-search" className="text-input search-input" type="search" value={reviewSearch} onChange={(event) => setReviewSearch(event.target.value)} placeholder="Search content across all properties..." />
          </div>

          <div id="publish-downloads" className={`downloads-panel ${downloads.visible ? "" : "is-hidden"}`}>
            <a id="download-all-link" className="ghost-button" href={downloads.href}>Download All Files</a>
            <p id="publish-download-note" className="downloads-note">{downloads.note}</p>
          </div>

          <div id="entity-list" className="entity-list">
            {reviewLoading ? <div className="empty-state">Loading review data...</div> : null}
            {!reviewLoading && !reviewData ? <div className="empty-state">Open a session to review extracted content.</div> : null}
            {!reviewLoading && reviewData && filteredEntities.length === 0 ? <div className="empty-state">No matching content found.</div> : null}

            {!reviewLoading &&
              filteredEntities.map(({ entity, index, items, forceOpen }) => {
                const isOpen = forceOpen || expandedEntities.has(entity.id);
                const itemEnabledCount = (entity.items || []).filter((entry) => entry.enabled !== false).length;
                const itemTotalCount = (entity.items || []).length;
                return (
                  <article key={entity.id} className={`card entity-card ${isOpen ? "is-open" : ""} ${entity.enabled === false ? "is-property-disabled" : ""}`}>
                    <div className="entity-header">
                      <button className="entity-header-main" type="button" onClick={() => toggleEntityOpen(entity.id)} aria-expanded={isOpen}>
                        <div className="entity-heading">
                          <span className="entity-marker">
                            <svg viewBox="0 0 24 24" aria-hidden="true">
                              <path d="M12 3v10"></path>
                              <path d="M8 7l4-4 4 4"></path>
                              <path d="M5 13a7 7 0 1 0 14 0"></path>
                            </svg>
                            <span>Entity {index + 1}</span>
                          </span>
                          <div className="entity-title-wrap">
                            <h3 className="entity-title">{entity.name || entity.suggested_name || "Property"}</h3>
                            <div className="entity-stats">
                              <span>
                                <svg viewBox="0 0 24 24" aria-hidden="true">
                                  <path d="M4.5 5.5h15"></path>
                                  <path d="M4.5 12h15"></path>
                                  <path d="M4.5 18.5h10"></path>
                                </svg>
                                {entity.stats?.pages || 0} pages
                              </span>
                              <span>{entity.stats?.images || 0} images</span>
                              <span>{entity.stats?.videos || 0} videos</span>
                              <span>{entity.stats?.files || 0} files</span>
                            </div>
                          </div>
                        </div>
                        <div className="entity-right">
                          <span className="enabled-pill">{itemEnabledCount}/{itemTotalCount} enabled</span>
                          <span className="entity-chevron">
                            <svg viewBox="0 0 24 24" aria-hidden="true">
                              <path d="m7 10 5 5 5-5"></path>
                            </svg>
                          </span>
                        </div>
                      </button>
                      <button className={`entity-property-button ${entity.enabled !== false ? "is-selected" : "is-skipped"}`} type="button" onClick={() => toggleEntityEnabled(entity.id)}>
                        {entity.enabled !== false ? "Selected" : "Skipped"}
                      </button>
                    </div>

                    <div className="entity-body">
                      <div className="entity-items">
                        {items.map((item) => (
                          <div key={item.id} className={`entity-item ${item.enabled !== false ? "" : "is-disabled"} ${entity.enabled !== false ? "" : "is-property-disabled"}`}>
                            <div className="entity-item-toggle">
                              <label className="item-switch">
                                <input type="checkbox" checked={item.enabled !== false} disabled={entity.enabled === false} onChange={() => toggleItemEnabled(entity.id, item.id)} />
                                <span className="item-switch-ui"></span>
                              </label>
                            </div>

                            <button className="entity-item-main" type="button" onClick={() => openEditor(entity.id, item.id)}>
                              <div className="entity-item-copy">
                                <span className="item-icon">{iconForItemType(item.type)}</span>
                                <div className="entity-item-text">
                                  <div className="entity-item-name">
                                    <span>{item.name || item.suggested_name || "Untitled"}</span>
                                    {item.display_path ? <span className="entity-item-path">{item.display_path}</span> : null}
                                  </div>
                                  {buildItemMeta(item) ? <div className="entity-item-meta">{buildItemMeta(item)}</div> : null}
                                </div>
                              </div>
                            </button>

                            <div className="entity-item-actions">
                              <button className={`label-button ${item.label ? "" : "is-empty"}`} type="button" onClick={() => openEditor(entity.id, item.id)}>
                                <svg viewBox="0 0 24 24" aria-hidden="true">
                                  <path d="M4.5 12.5 12 5l7.5 7.5-7.5 7.5z"></path>
                                </svg>
                                <span>{item.label || "Add Label"}</span>
                              </button>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </article>
                );
              })}
          </div>
        </section>
      </main>

      <div id="edit-modal" className={`modal ${editor ? "" : "is-hidden"}`} aria-hidden={editor ? "false" : "true"}>
        <div className="modal-backdrop" onClick={closeEditor}></div>
        <div className="modal-dialog" role="dialog" aria-modal="true" aria-labelledby="edit-modal-title">
          <div className="modal-header">
            <div>
              <h2 id="edit-modal-title">Edit Content</h2>
              <p id="edit-modal-subtitle">Review extracted metadata before publishing.</p>
            </div>
            <button className="icon-button modal-close" type="button" aria-label="Close modal" onClick={closeEditor}>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="m6 6 12 12"></path>
                <path d="M18 6 6 18"></path>
              </svg>
            </button>
          </div>

          <div id="modal-preview" className={`modal-preview ${editor && editor.previewUrl ? "" : "is-hidden"}`}>
            {editor?.previewUrl ? (
              <div className="modal-preview-frame">
                <img src={editor.previewUrl} alt={editorName || "Preview"} loading="lazy" />
              </div>
            ) : null}
          </div>

          <div className="modal-body">
            <div className="field-group">
              <label className="field-label" htmlFor="edit-name-input">Name</label>
              <input id="edit-name-input" className="text-input" type="text" value={editorName} onChange={(event) => setEditorName(event.target.value)} />
              <p id="suggested-name-hint" className="suggested-hint">Suggested: {editor?.suggestedName || "-"}</p>
            </div>

            <div id="edit-label-group" className="field-group">
              <label className="field-label" htmlFor="edit-label-input">Label</label>
              <input id="edit-label-input" className="text-input" type="text" value={editorLabel} onChange={(event) => setEditorLabel(event.target.value)} />
              <p id="suggested-label-hint" className="suggested-hint">Suggested: {editor?.suggestedLabel || "-"}</p>
            </div>
          </div>

          <div className="modal-actions">
            <button id="reset-suggested-button" className="ghost-button" type="button" onClick={resetEditorToSuggested}>Reset to Suggested</button>
            <button id="save-edit-button" className="primary-button primary-button-compact" type="button" onClick={saveEditor}>Save Changes</button>
          </div>
        </div>
      </div>
    </>
  );
}

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(<App />);
