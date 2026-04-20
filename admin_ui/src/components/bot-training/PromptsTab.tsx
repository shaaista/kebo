import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useToast } from "@/hooks/use-toast";
import { adminGet, adminSend } from "@/lib/adminApi";
import {
  Eye,
  Loader2,
  Pencil,
  RefreshCcw,
  RotateCcw,
  Sparkles,
  Trash2,
} from "lucide-react";

interface PromptListItem {
  key: string;
  source: "hotel" | "industry" | string;
  industry: string | null;
  has_override: boolean;
  variables: string[] | null;
  description: string | null;
  version: number;
}

interface PromptDetail {
  key: string;
  content: string;
  source: "hotel" | "industry" | string;
  industry: string | null;
  has_override: boolean;
  industry_default_content: string | null;
  variables: string[] | null;
  description: string | null;
  version: number;
}

interface PromptGroupsResponse {
  groups: Record<string, PromptListItem[]>;
}

interface PromptsTabProps {
  propertyCode: string;
}

const GROUP_ORDER = ["Orchestrator", "Service Writer", "Ticketing", "Chat", "Other"];

const GROUP_DESCRIPTIONS: Record<string, string> = {
  Orchestrator:
    "Routing, KB extraction, suggestions and answer-validation prompts the orchestrator uses every turn.",
  "Service Writer":
    "Briefing + rule snippets used to auto-generate per-service system prompts.",
  Ticketing: "Case-matcher and update-assessment prompts used by the ticketing flow.",
  Chat:
    "Pre-processing, intent classification, fallback generator, response polish and repair.",
  Other: "Uncategorised prompts.",
};

const KEY_LABELS: Record<string, string> = {
  "orchestrator.kb_chunk_scan": "KB chunk scan",
  "orchestrator.service_router": "Service router",
  "orchestrator.continuity_router": "Continuity router",
  "orchestrator.next_suggestions": "Next suggestions",
  "orchestrator.answer_first_guard": "Answer-first guard",
  "orchestrator.service_template": "Service fallback template",
  "service_writer.bot_briefing": "Bot briefing",
  "service_writer.form_mode_rules": "Form-mode rules",
  "service_writer.text_mode_rules": "Text-mode rules",
  "service_writer.multi_property_rule": "Multi-property rule",
  "service_writer.instructions": "Writer instructions",
  "ticketing.case_matcher": "Case matcher",
  "ticketing.expired_update_assessment": "Expired update check",
  "chat.preprocess": "Preprocess",
  "chat.classify_intent": "Intent classifier",
  "chat.generate_response": "Response generator",
  "chat.response_surface": "Response surface polish",
  "chat.response_repair": "Response repair",
  "chat.response_repair_system": "Response repair (system)",
};

const labelFor = (key: string): string => {
  if (KEY_LABELS[key]) return KEY_LABELS[key];
  const tail = key.includes(".") ? key.split(".").slice(1).join(".") : key;
  return tail
    .replace(/[._-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
};

const orderedGroupKeys = (groups: Record<string, PromptListItem[]>): string[] => {
  const present = Object.keys(groups);
  const ordered: string[] = [];
  for (const name of GROUP_ORDER) {
    if (present.includes(name)) ordered.push(name);
  }
  for (const name of present) {
    if (!ordered.includes(name)) ordered.push(name);
  }
  return ordered;
};

const PromptsTab = ({ propertyCode }: PromptsTabProps) => {
  const { toast } = useToast();
  const [loading, setLoading] = useState(false);
  const [groups, setGroups] = useState<Record<string, PromptListItem[]>>({});
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [openMode, setOpenMode] = useState<"view" | "edit" | "regen" | null>(null);
  const [detail, setDetail] = useState<PromptDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [editScope, setEditScope] = useState<"hotel" | "industry">("hotel");
  const [saving, setSaving] = useState(false);
  const [reverting, setReverting] = useState(false);
  const [regenInstruction, setRegenInstruction] = useState("");
  const [regenRunning, setRegenRunning] = useState(false);
  const [regenRewrite, setRegenRewrite] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!propertyCode) return;
    setLoading(true);
    try {
      const response = await adminGet<PromptGroupsResponse>("/prompts", propertyCode);
      setGroups(response?.groups || {});
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load prompts";
      toast({ title: "Could not load prompts", description: message, variant: "destructive" });
      setGroups({});
    } finally {
      setLoading(false);
    }
  }, [propertyCode, toast]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const closeDialog = useCallback(() => {
    setOpenKey(null);
    setOpenMode(null);
    setDetail(null);
    setEditContent("");
    setEditScope("hotel");
    setRegenInstruction("");
    setRegenRewrite(null);
  }, []);

  const loadDetail = useCallback(
    async (key: string) => {
      setDetailLoading(true);
      try {
        const response = await adminGet<PromptDetail>(
          `/prompts/${encodeURIComponent(key)}`,
          propertyCode,
        );
        setDetail(response);
        setEditContent(response?.content || "");
        setEditScope(response?.has_override ? "hotel" : "hotel");
        setRegenRewrite(null);
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to load prompt";
        toast({ title: "Could not load prompt", description: message, variant: "destructive" });
        closeDialog();
      } finally {
        setDetailLoading(false);
      }
    },
    [propertyCode, toast, closeDialog],
  );

  const openFor = useCallback(
    (key: string, mode: "view" | "edit" | "regen") => {
      setOpenKey(key);
      setOpenMode(mode);
      void loadDetail(key);
    },
    [loadDetail],
  );

  const saveOverride = useCallback(async () => {
    if (!detail) return;
    setSaving(true);
    try {
      await adminSend(
        "PUT",
        `/prompts/${encodeURIComponent(detail.key)}`,
        {
          content: editContent,
          description: detail.description ?? undefined,
          variables: detail.variables ?? undefined,
          scope: editScope,
        },
        propertyCode,
      );
      toast({
        title: editScope === "industry" ? "Industry default updated" : "Hotel override saved",
        description: detail.key,
      });
      await refresh();
      closeDialog();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to save prompt";
      toast({ title: "Save failed", description: message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  }, [detail, editContent, editScope, propertyCode, toast, refresh, closeDialog]);

  const revertToDefault = useCallback(async () => {
    if (!detail) return;
    setReverting(true);
    try {
      await adminSend(
        "DELETE",
        `/prompts/${encodeURIComponent(detail.key)}`,
        undefined,
        propertyCode,
      );
      toast({ title: "Reverted to industry default", description: detail.key });
      await refresh();
      closeDialog();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to revert";
      toast({ title: "Revert failed", description: message, variant: "destructive" });
    } finally {
      setReverting(false);
    }
  }, [detail, propertyCode, toast, refresh, closeDialog]);

  const runRegenerate = useCallback(async () => {
    if (!detail) return;
    const instruction = regenInstruction.trim();
    if (!instruction) {
      toast({
        title: "Instruction required",
        description: "Tell the assistant what to change.",
        variant: "destructive",
      });
      return;
    }
    setRegenRunning(true);
    setRegenRewrite(null);
    try {
      const response = await adminSend<{ rewrite: string }>(
        "POST",
        `/prompts/${encodeURIComponent(detail.key)}/regenerate`,
        { instruction },
        propertyCode,
      );
      setRegenRewrite(response?.rewrite || "");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to regenerate";
      toast({ title: "Regenerate failed", description: message, variant: "destructive" });
    } finally {
      setRegenRunning(false);
    }
  }, [detail, regenInstruction, propertyCode, toast]);

  const acceptRewrite = useCallback(() => {
    if (regenRewrite == null) return;
    setEditContent(regenRewrite);
    setOpenMode("edit");
    setRegenRewrite(null);
    setRegenInstruction("");
  }, [regenRewrite]);

  const regenerateAllServices = useCallback(async () => {
    setSaving(true);
    try {
      const result = await adminSend<{ regenerated: number; skipped_locked: number }>(
        "POST",
        "/services/regenerate-all-prompts",
        undefined,
        propertyCode,
      );
      toast({
        title: "Service prompts regenerated",
        description: `${result?.regenerated ?? 0} regenerated, ${result?.skipped_locked ?? 0} locked & skipped`,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to regenerate service prompts";
      toast({ title: "Regenerate failed", description: message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  }, [propertyCode, toast]);

  const sortedGroupNames = useMemo(() => orderedGroupKeys(groups), [groups]);

  const totalCount = useMemo(
    () => Object.values(groups).reduce((sum, list) => sum + list.length, 0),
    [groups],
  );

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
          <div>
            <CardTitle>Prompts &amp; Behavior</CardTitle>
            <p className="mt-1 text-sm text-muted-foreground">
              Every LLM prompt the bot uses, with industry defaults and per-hotel overrides.
              {totalCount > 0 ? ` ${totalCount} prompts loaded.` : null}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
              {loading ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <RefreshCcw className="mr-2 h-4 w-4" />
              )}
              Refresh
            </Button>
            <Button size="sm" onClick={regenerateAllServices} disabled={saving}>
              <Sparkles className="mr-2 h-4 w-4" />
              Regenerate non-locked services
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {loading && totalCount === 0 ? (
            <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading prompts…
            </div>
          ) : sortedGroupNames.length === 0 ? (
            <div className="py-8 text-sm text-muted-foreground">
              No prompts in the registry yet. The seed runs on server startup — try refreshing.
            </div>
          ) : (
            <Accordion type="multiple" defaultValue={sortedGroupNames} className="w-full">
              {sortedGroupNames.map((groupName) => {
                const items = groups[groupName] || [];
                const overrideCount = items.filter((p) => p.has_override).length;
                return (
                  <AccordionItem key={groupName} value={groupName}>
                    <AccordionTrigger className="text-left">
                      <span className="flex items-center gap-2">
                        <span className="font-semibold">{groupName}</span>
                        <Badge variant="secondary">{items.length}</Badge>
                        {overrideCount > 0 ? (
                          <Badge>{overrideCount} override{overrideCount === 1 ? "" : "s"}</Badge>
                        ) : null}
                      </span>
                    </AccordionTrigger>
                    <AccordionContent>
                      <p className="mb-3 text-xs text-muted-foreground">
                        {GROUP_DESCRIPTIONS[groupName] || ""}
                      </p>
                      <div className="divide-y rounded-md border">
                        {items.map((item) => (
                          <div
                            key={item.key}
                            className="flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between"
                          >
                            <div className="min-w-0">
                              <div className="flex flex-wrap items-center gap-2">
                                <span className="text-sm font-medium">{labelFor(item.key)}</span>
                                {item.has_override ? (
                                  <Badge>Hotel override ★</Badge>
                                ) : (
                                  <Badge variant="secondary">Industry default</Badge>
                                )}
                                {item.industry ? (
                                  <Badge variant="outline">{item.industry}</Badge>
                                ) : null}
                              </div>
                              <div className="mt-1 truncate text-xs text-muted-foreground">
                                <code className="font-mono">{item.key}</code>
                                {item.description ? <> — {item.description}</> : null}
                              </div>
                            </div>
                            <div className="flex flex-shrink-0 items-center gap-2">
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => openFor(item.key, "view")}
                              >
                                <Eye className="mr-1 h-3.5 w-3.5" /> View
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => openFor(item.key, "edit")}
                              >
                                <Pencil className="mr-1 h-3.5 w-3.5" /> Edit
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => openFor(item.key, "regen")}
                              >
                                <Sparkles className="mr-1 h-3.5 w-3.5" /> Regen
                              </Button>
                            </div>
                          </div>
                        ))}
                        {items.length === 0 ? (
                          <div className="p-3 text-xs text-muted-foreground">
                            No prompts in this group.
                          </div>
                        ) : null}
                      </div>
                    </AccordionContent>
                  </AccordionItem>
                );
              })}
            </Accordion>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Onboarding prompts</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          The hotel&apos;s base system prompt, classifier prompt and response style live in the
          existing onboarding wizard and remain edited there — see the <strong>Setup Wizard</strong> tab.
          They are stored per-hotel in the business config and are intentionally not duplicated here.
        </CardContent>
      </Card>

      <Dialog
        open={openKey !== null && openMode !== null}
        onOpenChange={(next) => {
          if (!next) closeDialog();
        }}
      >
        <DialogContent className="max-h-[90vh] max-w-3xl overflow-hidden">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {openMode === "view" ? <Eye className="h-4 w-4" /> : null}
              {openMode === "edit" ? <Pencil className="h-4 w-4" /> : null}
              {openMode === "regen" ? <Sparkles className="h-4 w-4" /> : null}
              {detail ? labelFor(detail.key) : openKey || ""}
            </DialogTitle>
            <DialogDescription>
              {detail ? (
                <span className="flex flex-wrap items-center gap-2 text-xs">
                  <code className="font-mono">{detail.key}</code>
                  {detail.has_override ? (
                    <Badge>Hotel override ★</Badge>
                  ) : (
                    <Badge variant="secondary">Industry default</Badge>
                  )}
                  {detail.industry ? (
                    <Badge variant="outline">{detail.industry}</Badge>
                  ) : null}
                  <span>v{detail.version}</span>
                </span>
              ) : (
                <span>Loading…</span>
              )}
            </DialogDescription>
          </DialogHeader>

          {detailLoading || !detail ? (
            <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : openMode === "view" ? (
            <div className="space-y-3 overflow-y-auto pr-1">
              {detail.variables && detail.variables.length > 0 ? (
                <div className="text-xs">
                  <span className="font-medium">Variables: </span>
                  {detail.variables.map((v) => (
                    <code key={v} className="mr-1 rounded bg-muted px-1 py-0.5 font-mono">
                      {`{${v}}`}
                    </code>
                  ))}
                </div>
              ) : null}
              <div>
                <Label className="text-xs">Effective content</Label>
                <Textarea
                  readOnly
                  value={detail.content}
                  className="mt-1 h-[40vh] font-mono text-xs"
                />
              </div>
              {detail.has_override && detail.industry_default_content ? (
                <div>
                  <Label className="text-xs">Industry default (for comparison)</Label>
                  <Textarea
                    readOnly
                    value={detail.industry_default_content}
                    className="mt-1 h-[20vh] font-mono text-xs"
                  />
                </div>
              ) : null}
            </div>
          ) : openMode === "edit" ? (
            <div className="space-y-3 overflow-y-auto pr-1">
              {detail.variables && detail.variables.length > 0 ? (
                <div className="text-xs">
                  <span className="font-medium">Available variables: </span>
                  {detail.variables.map((v) => (
                    <code key={v} className="mr-1 rounded bg-muted px-1 py-0.5 font-mono">
                      {`{${v}}`}
                    </code>
                  ))}
                  <p className="mt-1 text-muted-foreground">
                    Use <code className="font-mono">{`{name}`}</code> placeholders. Literal braces
                    must be doubled (<code className="font-mono">{`{{`}</code> /{" "}
                    <code className="font-mono">{`}}`}</code>).
                  </p>
                </div>
              ) : null}
              <div>
                <Label className="text-xs" htmlFor="prompt-edit-content">
                  Prompt content
                </Label>
                <Textarea
                  id="prompt-edit-content"
                  value={editContent}
                  onChange={(e) => setEditContent(e.target.value)}
                  className="mt-1 h-[50vh] font-mono text-xs"
                />
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-medium">Save as:</span>
                <Button
                  size="sm"
                  variant={editScope === "hotel" ? "default" : "outline"}
                  onClick={() => setEditScope("hotel")}
                >
                  Hotel override
                </Button>
                <Button
                  size="sm"
                  variant={editScope === "industry" ? "default" : "outline"}
                  onClick={() => setEditScope("industry")}
                >
                  Industry default
                </Button>
                <span className="text-muted-foreground">
                  {editScope === "industry"
                    ? "Affects every hotel in this industry."
                    : "Affects only this hotel."}
                </span>
              </div>
            </div>
          ) : openMode === "regen" ? (
            <div className="space-y-3 overflow-y-auto pr-1">
              <div>
                <Label className="text-xs" htmlFor="prompt-regen-instruction">
                  Describe what you want to change
                </Label>
                <Textarea
                  id="prompt-regen-instruction"
                  placeholder="e.g. make it more formal and British, prefer numbered steps over bullets"
                  value={regenInstruction}
                  onChange={(e) => setRegenInstruction(e.target.value)}
                  className="mt-1 h-24 text-sm"
                />
                <Button
                  size="sm"
                  className="mt-2"
                  onClick={runRegenerate}
                  disabled={regenRunning}
                >
                  {regenRunning ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Sparkles className="mr-2 h-4 w-4" />
                  )}
                  Generate rewrite
                </Button>
              </div>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div>
                  <Label className="text-xs">Current</Label>
                  <Textarea
                    readOnly
                    value={detail.content}
                    className="mt-1 h-[40vh] font-mono text-xs"
                  />
                </div>
                <div>
                  <Label className="text-xs">Proposed rewrite</Label>
                  <Textarea
                    readOnly
                    value={regenRewrite ?? ""}
                    placeholder={regenRunning ? "Generating…" : "Run a regenerate to see a proposal."}
                    className="mt-1 h-[40vh] font-mono text-xs"
                  />
                </div>
              </div>
            </div>
          ) : null}

          <DialogFooter className="flex flex-wrap items-center gap-2">
            {openMode === "edit" && detail?.has_override ? (
              <Button
                variant="outline"
                size="sm"
                onClick={revertToDefault}
                disabled={reverting}
              >
                {reverting ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Trash2 className="mr-2 h-4 w-4" />
                )}
                Revert to industry default
              </Button>
            ) : null}
            {openMode === "regen" && regenRewrite != null ? (
              <Button variant="outline" size="sm" onClick={acceptRewrite}>
                <RotateCcw className="mr-2 h-4 w-4" /> Move rewrite into editor
              </Button>
            ) : null}
            <div className="flex-1" />
            <Button variant="ghost" size="sm" onClick={closeDialog}>
              {openMode === "view" ? "Close" : "Cancel"}
            </Button>
            {openMode === "edit" ? (
              <Button size="sm" onClick={saveOverride} disabled={saving || !detail}>
                {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                Save
              </Button>
            ) : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default PromptsTab;
