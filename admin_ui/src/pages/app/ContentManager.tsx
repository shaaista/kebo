import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Download,
  Loader2,
  RefreshCcw,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  adminGet,
  adminSend,
  getActivePropertyCode,
  normalizePropertyCode,
  setActivePropertyCode,
} from "@/lib/adminApi";
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

type PropertyOption = {
  code: string;
  name: string;
  city: string;
};

type HotelImageRow = {
  id: number;
  hotel_id: number;
  title: string;
  description: string;
  image_url: string;
  category: string;
  tags: string[];
  source_label: string;
  is_active: boolean;
  priority: number;
};

type EditableHotelImage = HotelImageRow & {
  draftTitle: string;
  draftImageUrl: string;
  draftSourceLabel: string;
  dirty: boolean;
};

type HotelImagesResponse = {
  hotel?: {
    id?: number;
    code?: string;
    name?: string;
    city?: string;
  };
  images?: HotelImageRow[];
  total?: number;
};

type HotelImageUpdateResponse = {
  image?: HotelImageRow;
};

function toEditableImage(row: HotelImageRow): EditableHotelImage {
  return {
    ...row,
    draftTitle: String(row.title || ""),
    draftImageUrl: String(row.image_url || ""),
    draftSourceLabel: String(row.source_label || ""),
    dirty: false,
  };
}

function isHttpImageUrl(value: string): boolean {
  const normalized = String(value || "").trim().toLowerCase();
  return normalized.startsWith("http://") || normalized.startsWith("https://");
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
        i.id === id ? { ...i, enabled: checked } : i,
      ),
    });
  }

  const enabledCount = pages.filter((p) => p.enabled !== false).length;

  return (
    <Card className="mb-3">
      <CardHeader className="px-4 py-3">
        <div className="flex items-center gap-3">
          <Checkbox
            id={`entity-${entity.id}`}
            checked={entity.enabled !== false}
            onCheckedChange={(v) => toggleEntity(!!v)}
          />
          <Label
            htmlFor={`entity-${entity.id}`}
            className="flex-1 cursor-pointer text-base font-semibold"
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
        <CardContent className="px-4 pb-4 pt-0">
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
                    className="cursor-pointer text-sm"
                  >
                    <span className="font-medium">{itemDisplayName(page)}</span>
                    {page.url && (
                      <span className="block max-w-sm truncate text-xs text-muted-foreground">
                        {page.url}
                      </span>
                    )}
                  </Label>
                </li>
              ))}
            </ul>
          )}
          {assets.length > 0 && (
            <p className="mt-2 text-xs text-muted-foreground">
              +{assets.length} media asset{assets.length !== 1 ? "s" : ""}
            </p>
          )}
        </CardContent>
      )}
    </Card>
  );
}

export default function ContentManager() {
  const [propertyCode, setPropertyCode] = useState(getActivePropertyCode());
  const [propertyOptions, setPropertyOptions] = useState<PropertyOption[]>([]);
  const [loadingProperties, setLoadingProperties] = useState(true);
  const [hotelImages, setHotelImages] = useState<EditableHotelImage[]>([]);
  const [loadingHotelImages, setLoadingHotelImages] = useState(false);
  const [hotelImagesStatus, setHotelImagesStatus] = useState<"idle" | "ok" | "err">("idle");
  const [hotelImagesMsg, setHotelImagesMsg] = useState("");
  const [savingImageId, setSavingImageId] = useState<number | null>(null);
  const [deletingImageId, setDeletingImageId] = useState<number | null>(null);
  const [imageRowMessages, setImageRowMessages] = useState<Record<number, { type: "ok" | "err"; text: string }>>({});
  const [newImageTitle, setNewImageTitle] = useState("");
  const [newImageUrl, setNewImageUrl] = useState("");
  const [newImageSourceLabel, setNewImageSourceLabel] = useState("");
  const [creatingImage, setCreatingImage] = useState(false);
  const [previewImage, setPreviewImage] = useState<{ url: string; title: string } | null>(null);

  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [loadingJobs, setLoadingJobs] = useState(true);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  const [reviewPayload, setReviewPayload] = useState<ReviewPayload | null>(null);
  const [loadingReview, setLoadingReview] = useState(false);
  const [publishStatus, setPublishStatus] = useState<"idle" | "busy" | "ok" | "err">("idle");
  const [publishMsg, setPublishMsg] = useState("");

  const fetchProperties = useCallback(async () => {
    setLoadingProperties(true);
    try {
      const payload = await adminGet<{ properties?: Array<{ id?: string; code?: string; name?: string; city?: string }> }>(
        "/properties",
        propertyCode,
      );
      const rows = Array.isArray(payload?.properties)
        ? payload.properties
            .map((row) => {
              const code = normalizePropertyCode(String(row?.id || row?.code || ""));
              return {
                code,
                name: String(row?.name || code).trim() || code,
                city: String(row?.city || "").trim(),
              };
            })
            .filter((row) => row.code)
        : [];

      const mergedRows =
        rows.length > 0
          ? rows
          : [{ code: normalizePropertyCode(propertyCode) || "default", name: normalizePropertyCode(propertyCode) || "default", city: "" }];
      setPropertyOptions(mergedRows);

      const activeCode = normalizePropertyCode(propertyCode) || "default";
      const hasActive = mergedRows.some((row) => row.code === activeCode);
      if (!hasActive && mergedRows[0]?.code) {
        setPropertyCode(mergedRows[0].code);
        setActivePropertyCode(mergedRows[0].code);
      }
    } catch {
      const fallbackCode = normalizePropertyCode(propertyCode) || "default";
      setPropertyOptions([{ code: fallbackCode, name: fallbackCode, city: "" }]);
    } finally {
      setLoadingProperties(false);
    }
  }, [propertyCode]);

  const fetchHotelImages = useCallback(async (targetPropertyCode?: string) => {
    const scopedCode = normalizePropertyCode(targetPropertyCode || propertyCode) || "default";
    setLoadingHotelImages(true);
    setHotelImagesStatus("idle");
    setHotelImagesMsg("");
    setImageRowMessages({});
    try {
      const payload = await adminGet<HotelImagesResponse>("/content/hotel-images", scopedCode);
      const rows = Array.isArray(payload?.images) ? payload.images : [];
      setHotelImages(rows.map(toEditableImage));
    } catch (e) {
      setHotelImages([]);
      setHotelImagesStatus("err");
      setHotelImagesMsg(e instanceof Error ? e.message : "Failed to load hotel images");
    } finally {
      setLoadingHotelImages(false);
    }
  }, [propertyCode]);

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

  useEffect(() => {
    fetchProperties();
  }, [fetchProperties]);

  useEffect(() => {
    const scopedCode = normalizePropertyCode(propertyCode) || "default";
    setActivePropertyCode(scopedCode);
    fetchHotelImages(scopedCode);
  }, [propertyCode, fetchHotelImages]);

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
        e.id === updated.id ? updated : e,
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

  function updateImageDraft(
    imageId: number,
    field: "draftTitle" | "draftImageUrl" | "draftSourceLabel",
    value: string,
  ) {
    setHotelImages((prev) =>
      prev.map((row) => {
        if (row.id !== imageId) return row;
        const nextDraftTitle = field === "draftTitle" ? value : row.draftTitle;
        const nextDraftImageUrl = field === "draftImageUrl" ? value : row.draftImageUrl;
        const nextDraftSourceLabel = field === "draftSourceLabel" ? value : row.draftSourceLabel;
        const dirty =
          nextDraftTitle.trim() !== row.title ||
          nextDraftImageUrl.trim() !== row.image_url ||
          nextDraftSourceLabel.trim() !== (row.source_label || "");
        return {
          ...row,
          draftTitle: nextDraftTitle,
          draftImageUrl: nextDraftImageUrl,
          draftSourceLabel: nextDraftSourceLabel,
          dirty,
        };
      }),
    );
    setImageRowMessages((prev) => {
      if (!(imageId in prev)) return prev;
      const next = { ...prev };
      delete next[imageId];
      return next;
    });
  }

  async function saveHotelImage(row: EditableHotelImage) {
    const nextTitle = row.draftTitle.trim();
    const nextImageUrl = row.draftImageUrl.trim();
    const nextSourceLabel = row.draftSourceLabel.trim();
    if (!nextTitle) {
      setImageRowMessages((prev) => ({
        ...prev,
        [row.id]: { type: "err", text: "Title is required" },
      }));
      return;
    }
    if (!isHttpImageUrl(nextImageUrl)) {
      setImageRowMessages((prev) => ({
        ...prev,
        [row.id]: { type: "err", text: "Image URL must start with http:// or https://" },
      }));
      return;
    }

    setSavingImageId(row.id);
    try {
      const payload = await adminSend<HotelImageUpdateResponse>(
        "PUT",
        `/content/hotel-images/${row.id}`,
        {
          title: nextTitle,
          image_url: nextImageUrl,
          source_label: nextSourceLabel || null,
        },
        propertyCode,
      );
      const updated = payload?.image;
      if (updated) {
        setHotelImages((prev) =>
          prev.map((item) => (item.id === row.id ? toEditableImage(updated) : item)),
        );
      } else {
        setHotelImages((prev) =>
          prev.map((item) =>
            item.id === row.id
              ? {
                  ...item,
                  title: nextTitle,
                  image_url: nextImageUrl,
                  source_label: nextSourceLabel,
                  draftTitle: nextTitle,
                  draftImageUrl: nextImageUrl,
                  draftSourceLabel: nextSourceLabel,
                  dirty: false,
                }
              : item,
          ),
        );
      }
      setImageRowMessages((prev) => ({
        ...prev,
        [row.id]: { type: "ok", text: "Saved" },
      }));
      setHotelImagesStatus("ok");
      setHotelImagesMsg("Image changes saved");
    } catch (e) {
      setImageRowMessages((prev) => ({
        ...prev,
        [row.id]: { type: "err", text: e instanceof Error ? e.message : "Save failed" },
      }));
    } finally {
      setSavingImageId(null);
    }
  }

  async function createHotelImage() {
    const title = newImageTitle.trim();
    const imageUrl = newImageUrl.trim();
    const sourceLabel = newImageSourceLabel.trim();
    if (!title) {
      setHotelImagesStatus("err");
      setHotelImagesMsg("Title is required");
      return;
    }
    if (!isHttpImageUrl(imageUrl)) {
      setHotelImagesStatus("err");
      setHotelImagesMsg("Image URL must start with http:// or https://");
      return;
    }

    setCreatingImage(true);
    try {
      const payload = await adminSend<HotelImageUpdateResponse>(
        "POST",
        "/content/hotel-images",
        {
          title,
          image_url: imageUrl,
          source_label: sourceLabel || null,
        },
        propertyCode,
      );
      const created = payload?.image;
      if (created) {
        setHotelImages((prev) => [toEditableImage(created), ...prev]);
      } else {
        await fetchHotelImages(propertyCode);
      }
      setNewImageTitle("");
      setNewImageUrl("");
      setNewImageSourceLabel("");
      setHotelImagesStatus("ok");
      setHotelImagesMsg("Image added");
    } catch (e) {
      setHotelImagesStatus("err");
      setHotelImagesMsg(e instanceof Error ? e.message : "Failed to add image");
    } finally {
      setCreatingImage(false);
    }
  }

  async function deleteHotelImage(imageId: number) {
    const confirmed = window.confirm("Delete this image permanently from DB?");
    if (!confirmed) return;

    setDeletingImageId(imageId);
    try {
      await adminSend<{ deleted?: boolean }>(
        "DELETE",
        `/content/hotel-images/${imageId}`,
        undefined,
        propertyCode,
      );
      setHotelImages((prev) => prev.filter((row) => row.id !== imageId));
      setImageRowMessages((prev) => {
        if (!(imageId in prev)) return prev;
        const next = { ...prev };
        delete next[imageId];
        return next;
      });
      setHotelImagesStatus("ok");
      setHotelImagesMsg("Image deleted");
    } catch (e) {
      setHotelImagesStatus("err");
      setHotelImagesMsg(e instanceof Error ? e.message : "Failed to delete image");
    } finally {
      setDeletingImageId(null);
    }
  }

  async function refreshHotelImageManager() {
    await fetchProperties();
    await fetchHotelImages(propertyCode);
  }

  const selectedJob = jobs.find((j) => j.job_id === selectedJobId);

  return (
    <div className="max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Content Manager</h1>
        <p className="text-muted-foreground">
          Manage hotel images and review extracted content in one place.
        </p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">Hotel Image Manager</CardTitle>
            <Button variant="ghost" size="sm" onClick={refreshHotelImageManager}>
              <RefreshCcw className="h-3.5 w-3.5" />
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="content-hotel-picker">Select Hotel</Label>
            {loadingProperties ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading hotels...
              </div>
            ) : (
              <select
                id="content-hotel-picker"
                className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                value={propertyCode}
                onChange={(event) => {
                  const nextCode = normalizePropertyCode(event.target.value);
                  if (!nextCode) return;
                  setPropertyCode(nextCode);
                  setActivePropertyCode(nextCode);
                }}
              >
                {propertyOptions.map((row) => (
                  <option key={row.code} value={row.code}>
                    {[row.code, row.name, row.city].filter(Boolean).join(" | ")}
                  </option>
                ))}
              </select>
            )}
          </div>

          <div className="rounded-md border p-3">
            <div className="mb-2 text-sm font-medium">Add New Image</div>
            <div className="grid gap-2 md:grid-cols-3">
              <div className="space-y-1">
                <Label htmlFor="new-image-title">Title</Label>
                <Input
                  id="new-image-title"
                  value={newImageTitle}
                  onChange={(event) => setNewImageTitle(event.target.value)}
                  placeholder="e.g. Poolside Lounge"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="new-image-url">Image URL</Label>
                <Input
                  id="new-image-url"
                  value={newImageUrl}
                  onChange={(event) => setNewImageUrl(event.target.value)}
                  placeholder="https://..."
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="new-image-source-label">Label</Label>
                <Input
                  id="new-image-source-label"
                  value={newImageSourceLabel}
                  onChange={(event) => setNewImageSourceLabel(event.target.value)}
                  placeholder="optional source label"
                />
              </div>
            </div>
            <div className="mt-3">
              <Button
                size="sm"
                onClick={createHotelImage}
                disabled={creatingImage}
              >
                {creatingImage ? (
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                ) : null}
                Add Image
              </Button>
            </div>
          </div>

          {loadingHotelImages ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading images...
            </div>
          ) : hotelImages.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No images found for this hotel.
            </p>
          ) : (
            <ul className="space-y-3">
              {hotelImages.map((row) => (
                <li key={row.id} className="rounded-lg border p-3">
                  <div className="grid items-start gap-3 md:grid-cols-[220px_1fr]">
                    <div className="self-start rounded-md border bg-muted p-2">
                      {row.draftImageUrl ? (
                        <button
                          type="button"
                          onClick={() =>
                            setPreviewImage({
                              url: row.draftImageUrl,
                              title: row.draftTitle || `Hotel image ${row.id}`,
                            })
                          }
                          className="block w-full"
                        >
                          <img
                            src={row.draftImageUrl}
                            alt={row.draftTitle || `Hotel image ${row.id}`}
                            className="block h-auto w-full cursor-zoom-in rounded-sm object-contain"
                            loading="lazy"
                          />
                        </button>
                      ) : (
                        <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
                          No image URL
                        </div>
                      )}
                    </div>
                    <div className="space-y-2">
                      <div className="space-y-1">
                        <Label htmlFor={`image-title-${row.id}`}>Title</Label>
                        <Input
                          id={`image-title-${row.id}`}
                          value={row.draftTitle}
                          onChange={(event) =>
                            updateImageDraft(row.id, "draftTitle", event.target.value)
                          }
                        />
                      </div>
                      <div className="space-y-1">
                        <Label htmlFor={`image-url-${row.id}`}>Image URL</Label>
                        <Input
                          id={`image-url-${row.id}`}
                          value={row.draftImageUrl}
                          onChange={(event) =>
                            updateImageDraft(row.id, "draftImageUrl", event.target.value)
                          }
                        />
                      </div>
                      <div className="space-y-1">
                        <Label htmlFor={`image-source-${row.id}`}>Label</Label>
                        <Input
                          id={`image-source-${row.id}`}
                          value={row.draftSourceLabel}
                          onChange={(event) =>
                            updateImageDraft(row.id, "draftSourceLabel", event.target.value)
                          }
                        />
                      </div>
                      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <span>ID #{row.id}</span>
                        {row.category ? <span>Category: {row.category}</span> : null}
                        {row.source_label ? <span>Source: {row.source_label}</span> : null}
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          size="sm"
                          onClick={() => saveHotelImage(row)}
                          disabled={!row.dirty || savingImageId === row.id || deletingImageId === row.id}
                        >
                          {savingImageId === row.id ? (
                            <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                          ) : null}
                          Save
                        </Button>
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => deleteHotelImage(row.id)}
                          disabled={savingImageId === row.id || deletingImageId === row.id}
                        >
                          {deletingImageId === row.id ? (
                            <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                          ) : (
                            <Trash2 className="mr-1 h-4 w-4" />
                          )}
                          Delete
                        </Button>
                        {imageRowMessages[row.id] ? (
                          <span
                            className={`text-xs ${
                              imageRowMessages[row.id].type === "ok"
                                ? "text-green-600"
                                : "text-red-600"
                            }`}
                          >
                            {imageRowMessages[row.id].text}
                          </span>
                        ) : null}
                      </div>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {hotelImagesStatus === "err" ? (
            <div className="flex items-center gap-2 text-sm text-red-600">
              <AlertTriangle className="h-4 w-4" />
              {hotelImagesMsg}
            </div>
          ) : hotelImagesStatus === "ok" ? (
            <div className="flex items-center gap-2 text-sm text-green-600">
              <CheckCircle2 className="h-4 w-4" />
              {hotelImagesMsg}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">Select a Crawl Session to Review</CardTitle>
            <Button variant="ghost" size="sm" onClick={fetchJobs}>
              <RefreshCcw className="h-3.5 w-3.5" />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {loadingJobs ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading sessions...
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
                    className={`w-full rounded-lg border px-4 py-3 text-left transition-colors ${
                      selectedJobId === job.job_id
                        ? "border-primary bg-primary/5"
                        : "hover:bg-muted/50"
                    }`}
                    onClick={() => loadReview(job.job_id)}
                  >
                    <div className="text-sm font-medium">
                      {job.session_name || "Unnamed Session"}
                    </div>
                    <div className="truncate text-xs text-muted-foreground">
                      {job.url} | {statusLabel(job.status)}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {selectedJobId && (
        <div>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-semibold">
              Review: {selectedJob?.session_name || selectedJobId.slice(0, 8)}
            </h2>
            {selectedJob?.can_download && (
              <Button variant="outline" size="sm" asChild>
                <a href={scraperApi.downloadUrl(selectedJobId)} download>
                  <Download className="mr-1 h-3.5 w-3.5" />
                  Download ZIP
                </a>
              </Button>
            )}
          </div>

          {loadingReview ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading review data...
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
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
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

      {previewImage ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          onClick={() => setPreviewImage(null)}
        >
          <div
            className="w-full max-w-5xl rounded-lg bg-background p-4 shadow-xl"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
            aria-modal="true"
          >
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="truncate text-sm font-medium">{previewImage.title}</div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setPreviewImage(null)}
              >
                Back
              </Button>
            </div>
            <div className="max-h-[75vh] overflow-auto rounded-md border bg-muted p-2">
              <img
                src={previewImage.url}
                alt={previewImage.title}
                className="mx-auto h-auto max-h-[72vh] w-auto object-contain"
              />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
