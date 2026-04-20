import { ChangeEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/hooks/use-toast";
import { adminGet, adminSend, makeServiceId } from "@/lib/adminApi";
import { Loader2, Pencil, Plus, RefreshCcw, Save, Sparkles, Trash2 } from "lucide-react";

interface PhaseRow {
  id: string;
  name: string;
  description: string;
  is_active: boolean;
  order: number;
}

type TicketingMode = "none" | "text" | "form";
type FormFieldType = "text" | "date" | "time" | "number" | "tel" | "email" | "textarea";

interface ServicePromptPack {
  extracted_knowledge?: string;
  ticketing_conditions?: string;
}

interface FormFieldDraft {
  id: string;
  label: string;
  type: FormFieldType;
  required: boolean;
  validation_prompt: string;
}

interface ServiceRow {
  id: string;
  name: string;
  type: string;
  description: string;
  is_active: boolean;
  is_builtin: boolean;
  phase_id?: string;
  ticketing_enabled?: boolean;
  ticketing_mode?: string;
  ticketing_policy?: string;
  service_prompt_pack?: ServicePromptPack;
  form_config?: {
    trigger_field?: { id?: string; label?: string; description?: string };
    pre_form_instructions?: string;
    fields?: Array<{
      id?: string;
      label?: string;
      type?: string;
      required?: boolean;
      validation_prompt?: string;
    }>;
  };
  generated_system_prompt?: string;
}

interface PrebuiltRow extends ServiceRow {
  is_installed?: boolean;
}

interface PhaseServiceDraft {
  id: string;
  name: string;
  type: string;
  userIntent: string;
  description: string;
  ticketingMode: TicketingMode;
  ticketingConditions: string;
  kbKnowledge: string;
}

interface FormDraft {
  triggerFieldId: string;
  triggerFieldLabel: string;
  triggerFieldDescription: string;
  preFormInstructions: string;
  fields: FormFieldDraft[];
}

interface PhasesTabProps {
  propertyCode: string;
}

const normalizeIdentifier = (value: string) =>
  String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "_")
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");

const asTicketingMode = (value: unknown): TicketingMode => {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === "form" || normalized === "text" || normalized === "none") return normalized;
  return "none";
};

const appendKnowledgeBlock = (existingText: string, incomingText: string): string => {
  const existing = String(existingText || "").trim();
  const incoming = String(incomingText || "").trim();
  if (!incoming) return existing;
  if (!existing) return incoming;
  if (existing.includes(incoming)) return existing;
  return `${existing}\n\n---\n\n${incoming}`;
};

const extractMenuDisplayTextFromPayload = (payload: { formatted_text?: unknown; menu_document?: unknown; fact_lines?: unknown }): string => {
  const menuDoc = payload?.menu_document && typeof payload.menu_document === "object" ? (payload.menu_document as { formatted_text?: unknown }) : null;
  const formatted = String(menuDoc?.formatted_text || payload?.formatted_text || "").trim();
  if (formatted) return formatted;
  const facts = Array.isArray(payload?.fact_lines)
    ? payload.fact_lines.map((entry) => String(entry || "").trim()).filter(Boolean)
    : [];
  return facts.join("\n").trim();
};

const emptyDraft = (): PhaseServiceDraft => ({
  id: "",
  name: "",
  type: "service",
  userIntent: "",
  description: "",
  ticketingMode: "none",
  ticketingConditions: "",
  kbKnowledge: "",
});

const emptyFormDraft = (): FormDraft => ({
  triggerFieldId: "",
  triggerFieldLabel: "",
  triggerFieldDescription: "",
  preFormInstructions: "",
  fields: [],
});

const defaultField = (): FormFieldDraft => ({
  id: "",
  label: "",
  type: "text",
  required: true,
  validation_prompt: "",
});

const PhasesTab = ({ propertyCode }: PhasesTabProps) => {
  const { toast } = useToast();

  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [phases, setPhases] = useState<PhaseRow[]>([]);
  const [phaseId, setPhaseId] = useState("pre_booking");
  const [allServices, setAllServices] = useState<ServiceRow[]>([]);
  const [phaseServices, setPhaseServices] = useState<ServiceRow[]>([]);
  const [prebuiltServices, setPrebuiltServices] = useState<PrebuiltRow[]>([]);
  const [selectedExistingServiceId, setSelectedExistingServiceId] = useState("");

  const [editingServiceId, setEditingServiceId] = useState<string | null>(null);
  const [createdServiceId, setCreatedServiceId] = useState("");
  const [isServiceIdManuallyEdited, setIsServiceIdManuallyEdited] = useState(false);
  const [draft, setDraft] = useState<PhaseServiceDraft>(() => emptyDraft());
  const [formDraft, setFormDraft] = useState<FormDraft>(() => emptyFormDraft());
  const [generatedPrompt, setGeneratedPrompt] = useState("");

  const [descriptionStatus, setDescriptionStatus] = useState("");
  const [ticketingStatus, setTicketingStatus] = useState("");
  const [kbStatus, setKbStatus] = useState("");
  const [promptStatus, setPromptStatus] = useState("");
  const [menuUploadStatus, setMenuUploadStatus] = useState<string[]>([]);
  const [phaseMenuFacts, setPhaseMenuFacts] = useState<string[]>([]);

  const selectedPhase = useMemo(
    () => phases.find((row) => normalizeIdentifier(row.id) === normalizeIdentifier(phaseId)),
    [phases, phaseId],
  );

  const existingCandidates = useMemo(() => {
    const inPhase = new Set(phaseServices.map((row) => normalizeIdentifier(row.id)));
    return allServices
      .filter((row) => !inPhase.has(normalizeIdentifier(row.id)))
      .sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)));
  }, [allServices, phaseServices]);

  const isEditing = Boolean(editingServiceId);

  const updateDraft = <K extends keyof PhaseServiceDraft>(key: K, value: PhaseServiceDraft[K]) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
  };

  const setFormField = <K extends keyof FormDraft>(key: K, value: FormDraft[K]) => {
    setFormDraft((prev) => ({ ...prev, [key]: value }));
  };

  const updateFormFieldDraft = (index: number, key: keyof FormFieldDraft, value: string | boolean) => {
    setFormDraft((prev) => ({
      ...prev,
      fields: prev.fields.map((field, idx) => (idx === index ? { ...field, [key]: value } : field)),
    }));
  };

  const appendMenuFacts = (nextFacts: string[]) => {
    setPhaseMenuFacts((prev) => {
      const merged = new Set(prev);
      for (const fact of nextFacts) {
        const line = String(fact || "").trim();
        if (line) merged.add(line);
      }
      return [...merged];
    });
  };

  const parseServiceRow = (row: Record<string, unknown>): ServiceRow => {
    const modeRaw =
      String(row.ticketing_mode || "").trim().toLowerCase() ||
      (row.ticketing_enabled !== false ? "text" : "none");
    return {
      id: String(row.id || ""),
      name: String(row.name || row.id || ""),
      type: String(row.type || "service"),
      description: String(row.description || ""),
      is_active: row.is_active !== false,
      is_builtin: row.is_builtin === true,
      phase_id: String(row.phase_id || ""),
      ticketing_enabled: row.ticketing_enabled !== false,
      ticketing_mode: asTicketingMode(modeRaw),
      ticketing_policy: String(row.ticketing_policy || ""),
      service_prompt_pack: (row.service_prompt_pack as ServicePromptPack | undefined) || {},
      form_config: (row.form_config as ServiceRow["form_config"]) || undefined,
      generated_system_prompt: String(row.generated_system_prompt || ""),
    };
  };

  const normalizePhase = useCallback((value: string) => normalizeIdentifier(value), []);

  const fetchPhaseScopedData = useCallback(
    async (targetPhaseId: string) => {
      const normalized = normalizePhase(targetPhaseId) || "pre_booking";
      const [phaseServiceRows, prebuiltRows] = await Promise.all([
        adminGet<Array<Record<string, unknown>>>(
          `/config/phases/${encodeURIComponent(normalized)}/services`,
          propertyCode,
        ),
        adminGet<Array<Record<string, unknown>>>(
          `/config/phases/${encodeURIComponent(normalized)}/prebuilt-services`,
          propertyCode,
        ),
      ]);
      setPhaseServices((Array.isArray(phaseServiceRows) ? phaseServiceRows : []).map(parseServiceRow));
      setPrebuiltServices(
        (Array.isArray(prebuiltRows) ? prebuiltRows : []).map((row) => ({
          ...parseServiceRow(row),
          is_installed: row.is_installed === true,
        })),
      );
    },
    [propertyCode, normalizePhase],
  );

  const loadWorkspace = useCallback(
    async (preferredPhaseId?: string) => {
      setLoading(true);
      try {
        const [phaseRows, serviceRows] = await Promise.all([
          adminGet<Array<Record<string, unknown>>>("/config/phases", propertyCode),
          adminGet<Array<Record<string, unknown>>>("/config/services", propertyCode),
        ]);

        const normalizedPhases = (Array.isArray(phaseRows) ? phaseRows : []).map((row, index) => ({
          id: normalizePhase(String(row.id || `phase_${index + 1}`)) || `phase_${index + 1}`,
          name: String(row.name || "").trim() || `Phase ${index + 1}`,
          description: String(row.description || "").trim(),
          is_active: row.is_active !== false,
          order: Number(row.order || index + 1) || index + 1,
        }));

        const allRows = (Array.isArray(serviceRows) ? serviceRows : []).map(parseServiceRow);
        setPhases(normalizedPhases);
        setAllServices(allRows);

        const preferred = normalizePhase(preferredPhaseId || phaseId);
        const fallback = normalizePhase(normalizedPhases.find((row) => normalizePhase(row.id) === "pre_booking")?.id || "");
        const first = normalizePhase(normalizedPhases[0]?.id || "");
        const nextPhase = preferred || fallback || first || "pre_booking";

        setPhaseId(nextPhase);
        await fetchPhaseScopedData(nextPhase);
      } catch (error) {
        toast({
          title: "Failed to load phase workspace",
          description: String(error instanceof Error ? error.message : error),
          variant: "destructive",
        });
      } finally {
        setLoading(false);
      }
    },
    [propertyCode, phaseId, normalizePhase, fetchPhaseScopedData, toast],
  );

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  const handlePhaseChange = async (nextPhaseId: string) => {
    const normalized = normalizePhase(nextPhaseId) || "pre_booking";
    setPhaseId(normalized);
    setBusy(true);
    try {
      await fetchPhaseScopedData(normalized);
    } catch (error) {
      toast({
        title: "Failed to switch phase",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  const startEditing = (service: ServiceRow) => {
    const mode = asTicketingMode(
      service.ticketing_mode || (service.ticketing_enabled !== false ? "text" : "none"),
    );
    const pack = service.service_prompt_pack || {};
    const ticketingConditions =
      String(pack.ticketing_conditions || "").trim() || String(service.ticketing_policy || "").trim();

    setEditingServiceId(service.id);
    setCreatedServiceId(service.id);
    setGeneratedPrompt(String(service.generated_system_prompt || ""));
    setPromptStatus("");
    setDescriptionStatus("");
    setTicketingStatus("");
    setKbStatus("");
    setDraft({
      id: String(service.id || ""),
      name: String(service.name || ""),
      type: String(service.type || "service"),
      userIntent: "",
      description: String(service.description || ""),
      ticketingMode: mode,
      ticketingConditions,
      kbKnowledge: String(pack.extracted_knowledge || ""),
    });
    setIsServiceIdManuallyEdited(true);

    const cfg = service.form_config || {};
    const fields = Array.isArray(cfg.fields)
      ? cfg.fields.map((field) => ({
          id: normalizeIdentifier(String(field?.id || "")),
          label: String(field?.label || field?.id || "").trim(),
          type: (String(field?.type || "text").trim().toLowerCase() as FormFieldType) || "text",
          required: field?.required !== false,
          validation_prompt: String(field?.validation_prompt || "").trim(),
        }))
      : [];

    setFormDraft({
      triggerFieldId: normalizeIdentifier(String(cfg.trigger_field?.id || "")),
      triggerFieldLabel: String(cfg.trigger_field?.label || "").trim(),
      triggerFieldDescription: String(cfg.trigger_field?.description || "").trim(),
      preFormInstructions: String(cfg.pre_form_instructions || "").trim(),
      fields,
    });
  };

  const resetEditor = () => {
    setEditingServiceId(null);
    setCreatedServiceId("");
    setIsServiceIdManuallyEdited(false);
    setGeneratedPrompt("");
    setDescriptionStatus("");
    setTicketingStatus("");
    setKbStatus("");
    setPromptStatus("");
    setMenuUploadStatus([]);
    setPhaseMenuFacts([]);
    setDraft(emptyDraft());
    setFormDraft(emptyFormDraft());
  };

  const suggestDescription = async () => {
    const name = String(draft.name || "").trim();
    if (!name) {
      toast({ title: "Enter service name first", variant: "destructive" });
      return;
    }

    setDescriptionStatus("Refining description...");
    try {
      const payload = await adminSend<{ description?: string; source?: string }>(
        "POST",
        "/config/phases/service-description/suggest",
        {
          name,
          phase_id: normalizePhase(phaseId) || "pre_booking",
          user_intent: String(draft.userIntent || "").trim() || undefined,
        },
        propertyCode,
      );
      const description = String(payload?.description || "").trim();
      if (description) {
        updateDraft("description", description);
        setDescriptionStatus(payload?.source === "llm" ? "Description refined." : "Fallback description applied.");
      } else {
        setDescriptionStatus("Could not generate description.");
      }
    } catch (error) {
      setDescriptionStatus(String(error instanceof Error ? error.message : error));
    }
  };

  const suggestTicketingConditions = async () => {
    const name = String(draft.name || "").trim();
    if (!name) {
      toast({ title: "Enter service name first", variant: "destructive" });
      return;
    }

    setTicketingStatus("Suggesting ticketing conditions...");
    try {
      const payload = await adminSend<{ conditions?: string }>(
        "POST",
        "/config/phases/ticketing-conditions/suggest",
        {
          service_name: name,
          service_description: String(draft.description || "").trim(),
          current_conditions: String(draft.ticketingConditions || "").trim(),
        },
        propertyCode,
      );
      const conditions = String(payload?.conditions || "").trim();
      if (conditions) {
        updateDraft("ticketingConditions", conditions);
        setTicketingStatus("Ticketing conditions updated.");
      } else {
        setTicketingStatus("No suggestion returned.");
      }
    } catch (error) {
      setTicketingStatus(String(error instanceof Error ? error.message : error));
    }
  };

  const buildSuggestedFormDraft = (): FormDraft => {
    const scope = `${draft.name} ${draft.description} ${draft.ticketingConditions}`.toLowerCase();
    const pushUnique = (rows: FormFieldDraft[], row: FormFieldDraft) => {
      const normalized = normalizeIdentifier(row.id);
      if (!normalized) return;
      if (rows.some((item) => normalizeIdentifier(item.id) === normalized)) return;
      rows.push({ ...row, id: normalized });
    };

    const fields: FormFieldDraft[] = [];
    pushUnique(fields, { id: "full_name", label: "Full Name", type: "text", required: true, validation_prompt: "" });
    pushUnique(fields, { id: "phone", label: "Phone Number", type: "tel", required: true, validation_prompt: "" });
    if (/(email|mail)/.test(scope)) {
      pushUnique(fields, { id: "email", label: "Email", type: "email", required: false, validation_prompt: "" });
    }
    if (/(room|suite|accommodation|reservation|booking|check.?in|check.?out|stay)/.test(scope)) {
      pushUnique(fields, { id: "room_type", label: "Room Type", type: "text", required: true, validation_prompt: "" });
      pushUnique(fields, { id: "checkin_date", label: "Check-in Date", type: "date", required: true, validation_prompt: "" });
      pushUnique(fields, {
        id: "checkout_date",
        label: "Check-out Date",
        type: "date",
        required: true,
        validation_prompt: "",
      });
    }
    if (/(time|slot|timing)/.test(scope)) {
      pushUnique(fields, {
        id: "preferred_time",
        label: "Preferred Time",
        type: "time",
        required: false,
        validation_prompt: "",
      });
    }
    if (/(transport|airport|pickup|drop|cab|taxi)/.test(scope)) {
      pushUnique(fields, {
        id: "pickup_location",
        label: "Pickup Location",
        type: "text",
        required: true,
        validation_prompt: "",
      });
      pushUnique(fields, {
        id: "drop_location",
        label: "Drop Location",
        type: "text",
        required: true,
        validation_prompt: "",
      });
    }
    if (/(spa|massage|treatment|wellness)/.test(scope)) {
      pushUnique(fields, {
        id: "treatment_type",
        label: "Treatment Type",
        type: "text",
        required: true,
        validation_prompt: "",
      });
    }
    if (/(complaint|issue|problem|maintenance|support)/.test(scope)) {
      pushUnique(fields, {
        id: "issue_details",
        label: "Issue Details",
        type: "textarea",
        required: true,
        validation_prompt: "",
      });
    }
    pushUnique(fields, {
      id: "special_requests",
      label: "Special Requests",
      type: "textarea",
      required: false,
      validation_prompt: "",
    });

    const triggerBase = normalizeIdentifier(draft.name || "request") || "request";
    return {
      triggerFieldId: `${triggerBase}_type`,
      triggerFieldLabel: `${draft.name || "Service"} Type`,
      triggerFieldDescription: "Confirm the request type before showing the form.",
      preFormInstructions: "Confirm request details, then collect all required fields from this form.",
      fields,
    };
  };

  const isFormDraftEmpty = useMemo(() => {
    if (formDraft.triggerFieldId || formDraft.triggerFieldLabel || formDraft.triggerFieldDescription) return false;
    if (formDraft.preFormInstructions) return false;
    if (formDraft.fields.some((field) => normalizeIdentifier(field.id))) return false;
    return true;
  }, [formDraft]);

  useEffect(() => {
    if (draft.ticketingMode !== "form") return;
    if (!isFormDraftEmpty) return;
    setFormDraft(buildSuggestedFormDraft());
  }, [draft.ticketingMode, draft.name, draft.description, draft.ticketingConditions, isFormDraftEmpty]);

  const collectFormConfig = () => {
    const fields = formDraft.fields
      .filter((field) => normalizeIdentifier(field.id))
      .map((field) => ({
        id: normalizeIdentifier(field.id),
        label: String(field.label || field.id).trim(),
        type: String(field.type || "text").trim(),
        required: field.required !== false,
        validation_prompt: String(field.validation_prompt || "").trim(),
      }));

    const triggerId = normalizeIdentifier(formDraft.triggerFieldId);
    return {
      trigger_field: triggerId
        ? {
            id: triggerId,
            label: String(formDraft.triggerFieldLabel || triggerId).trim(),
            description: String(formDraft.triggerFieldDescription || "").trim(),
          }
        : {},
      fields,
      pre_form_instructions: String(formDraft.preFormInstructions || "").trim(),
    };
  };

  const uploadMenus = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;

    setBusy(true);
    setMenuUploadStatus(files.map((file) => `Processing ${file.name}...`));

    try {
      const statusRows: string[] = [];
      const appendedKnowledgeBlocks: string[] = [];
      const extractedFacts: string[] = [];
      for (const file of files) {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("service_name", String(draft.name || file.name));
        formData.append("max_facts", "150");

        try {
          const payload = await adminSend<{ fact_lines?: string[] }>(
            "POST",
            "/agent-builder/menu-ocr/scan",
            formData,
            propertyCode,
          );
          const facts = Array.isArray(payload?.fact_lines)
            ? payload.fact_lines.map((line) => String(line || "").trim()).filter(Boolean)
            : [];
          extractedFacts.push(...facts);
          const menuText = extractMenuDisplayTextFromPayload(payload || {});
          const blockToAppend = String(menuText || facts.join("\n") || "").trim();
          if (blockToAppend) {
            appendedKnowledgeBlocks.push(blockToAppend);
            setDraft((prev) => ({
              ...prev,
              kbKnowledge: appendKnowledgeBlock(prev.kbKnowledge, blockToAppend),
            }));
          }
          appendMenuFacts(facts);
          statusRows.push(`${file.name}: ${facts.length} fact(s) extracted.`);
        } catch (error) {
          statusRows.push(`${file.name}: ${String(error instanceof Error ? error.message : error)}`);
        }
      }
      const nextFactCount = new Set([...phaseMenuFacts, ...extractedFacts]).size;
      setMenuUploadStatus(statusRows);
      setKbStatus(
        appendedKnowledgeBlocks.length > 0 || extractedFacts.length > 0
          ? `${nextFactCount} menu fact(s) ready and menu content added below. Click Pull from KB to append KB knowledge.`
          : "Menu OCR finished. Click Pull from KB to merge with KB knowledge.",
      );
    } finally {
      setBusy(false);
    }
  };

  const pullKbKnowledge = async () => {
    if (!String(draft.name || "").trim()) {
      toast({ title: "Enter service name first", variant: "destructive" });
      return;
    }
    setBusy(true);
    setKbStatus("Extracting relevant knowledge from KB...");
    try {
      const payload = await adminSend<{ extracted_knowledge?: string; reason?: string }>(
        "POST",
        "/config/service-kb/preview-extract",
        {
          service_name: String(draft.name || "").trim(),
          service_description: String(draft.description || "").trim(),
          extraction_mode: "verbatim",
          chunk_chars: 18000,
          existing_menu_facts: phaseMenuFacts,
        },
        propertyCode,
      );

      const extracted = String(payload?.extracted_knowledge || "").trim();
      if (extracted) {
        const existing = String(draft.kbKnowledge || "").trim();
        updateDraft("kbKnowledge", existing ? `${existing}\n\n---\n\n${extracted}` : extracted);
        setKbStatus("KB knowledge added.");
      } else {
        setKbStatus(`No KB extract available (${String(payload?.reason || "no content")}).`);
      }
    } catch (error) {
      setKbStatus(`KB extraction failed: ${String(error instanceof Error ? error.message : error)}`);
    } finally {
      setBusy(false);
    }
  };

  const savePhaseService = async () => {
    const phaseValue = normalizePhase(phaseId) || "pre_booking";
    const name = String(draft.name || "").trim();
    const description = String(draft.description || "").trim();
    const type = String(draft.type || "service").trim() || "service";
    const normalizedInputId = normalizeIdentifier(draft.id || "");
    const generatedFromName = normalizeIdentifier(makeServiceId(name).replace(/-/g, "_"));
    const nextId = isEditing ? normalizeIdentifier(String(editingServiceId || "")) : normalizedInputId || generatedFromName;
    const ticketingMode = asTicketingMode(draft.ticketingMode);
    const ticketingEnabled = ticketingMode !== "none";
    const ticketingConditions = String(draft.ticketingConditions || "").trim();
    const kbKnowledge = String(draft.kbKnowledge || "").trim();

    if (!name) {
      toast({ title: "Service name is required", variant: "destructive" });
      return;
    }
    if (!description) {
      toast({ title: "Service description is required", variant: "destructive" });
      return;
    }
    if (!nextId) {
      toast({ title: "Unable to resolve service ID", variant: "destructive" });
      return;
    }

    if (!isEditing) {
      const duplicateId = allServices.some((row) => normalizeIdentifier(row.id) === nextId);
      if (duplicateId) {
        toast({
          title: "Service ID already exists",
          description: "Use Add Existing Service To Phase for existing services.",
          variant: "destructive",
        });
        return;
      }
      const duplicateName = allServices.some((row) => String(row.name || "").trim().toLowerCase() === name.toLowerCase());
      if (duplicateName) {
        toast({
          title: "Service name already exists",
          description: "Use Add Existing Service To Phase for existing services.",
          variant: "destructive",
        });
        return;
      }
    }

    const payload: Record<string, unknown> = {
      id: nextId,
      name,
      type,
      description,
      phase_id: phaseValue,
      is_active: true,
      is_builtin: false,
      ticketing_enabled: ticketingEnabled,
      ticketing_mode: ticketingMode,
      ticketing_policy: ticketingConditions,
    };

    if (kbKnowledge || ticketingConditions) {
      payload.service_prompt_pack = {
        source: "manual_override",
        generator: "admin_ui",
        version: 1,
        ...(kbKnowledge ? { extracted_knowledge: kbKnowledge } : {}),
        ...(ticketingConditions ? { ticketing_conditions: ticketingConditions } : {}),
      };
    }
    if (ticketingMode === "form") {
      payload.form_config = collectFormConfig();
    } else if (isEditing) {
      payload.form_config = {};
    }

    setBusy(true);
    try {
      if (isEditing) {
        await adminSend("PUT", `/config/services/${encodeURIComponent(nextId)}`, payload, propertyCode);
        toast({ title: "Service updated" });
      } else {
        await adminSend("POST", "/config/services", payload, propertyCode);
        toast({ title: "Phase service created" });
      }

      resetEditor();

      await loadWorkspace(phaseValue);
    } catch (error) {
      toast({
        title: isEditing ? "Failed to update service" : "Failed to create service",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  const regeneratePrompt = async () => {
    const target = normalizeIdentifier(createdServiceId || editingServiceId || "");
    if (!target) {
      toast({ title: "Save a service first", variant: "destructive" });
      return;
    }
    setBusy(true);
    setPromptStatus("Generating service prompt...");
    try {
      const payload = await adminSend<{ generated_system_prompt?: string }>(
        "POST",
        `/config/services/${encodeURIComponent(target)}/regenerate-prompt`,
        undefined,
        propertyCode,
      );
      const prompt = String(payload?.generated_system_prompt || "").trim();
      if (prompt) {
        setGeneratedPrompt(prompt);
        setPromptStatus("AI prompt regenerated.");
      } else {
        setPromptStatus("Prompt generation did not return text.");
      }
      await loadWorkspace(phaseId);
    } catch (error) {
      setPromptStatus(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy(false);
    }
  };

  const savePrompt = async () => {
    const target = normalizeIdentifier(createdServiceId || editingServiceId || "");
    const prompt = String(generatedPrompt || "").trim();
    if (!target) {
      toast({ title: "Save a service first", variant: "destructive" });
      return;
    }
    if (!prompt) {
      toast({ title: "Prompt cannot be empty", variant: "destructive" });
      return;
    }
    setBusy(true);
    try {
      await adminSend("PUT", `/config/services/${encodeURIComponent(target)}/prompt`, { prompt }, propertyCode);
      setPromptStatus("Prompt saved.");
      toast({ title: "Prompt saved" });
      await loadWorkspace(phaseId);
    } catch (error) {
      setPromptStatus(String(error instanceof Error ? error.message : error));
      toast({
        title: "Failed to save prompt",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  const addExistingServiceToPhase = async () => {
    const serviceId = normalizeIdentifier(selectedExistingServiceId);
    const targetPhase = normalizePhase(phaseId) || "pre_booking";
    if (!serviceId) {
      toast({ title: "Select an existing service first", variant: "destructive" });
      return;
    }
    setBusy(true);
    try {
      await adminSend(
        "PUT",
        `/config/services/${encodeURIComponent(serviceId)}`,
        { phase_id: targetPhase },
        propertyCode,
      );
      toast({ title: "Service mapped to phase" });
      setSelectedExistingServiceId("");
      await loadWorkspace(targetPhase);
    } catch (error) {
      toast({
        title: "Failed to map service",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  const addPrebuiltServiceToPhase = async (templateId: string) => {
    const normalizedTemplateId = normalizeIdentifier(templateId);
    if (!normalizedTemplateId) return;
    const targetPhase = normalizePhase(phaseId) || "pre_booking";
    const template = prebuiltServices.find((row) => normalizeIdentifier(row.id) === normalizedTemplateId);
    if (!template) {
      toast({ title: "Template not found", variant: "destructive" });
      return;
    }

    const existing = allServices.find((row) => normalizeIdentifier(row.id) === normalizedTemplateId);
    const payload = existing
      ? { phase_id: targetPhase, is_builtin: true, ticketing_enabled: template.ticketing_enabled !== false }
      : {
          id: normalizedTemplateId,
          name: String(template.name || normalizedTemplateId),
          type: String(template.type || "service"),
          description: String(template.description || ""),
          phase_id: targetPhase,
          is_active: true,
          is_builtin: true,
          ticketing_enabled: template.ticketing_enabled !== false,
        };

    setBusy(true);
    try {
      await adminSend(
        existing ? "PUT" : "POST",
        existing ? `/config/services/${encodeURIComponent(normalizedTemplateId)}` : "/config/services",
        payload,
        propertyCode,
      );
      toast({ title: "Prebuilt service added" });
      await loadWorkspace(targetPhase);
    } catch (error) {
      toast({
        title: "Failed to add prebuilt service",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  const updateServiceToggle = async (
    serviceId: string,
    updates: Record<string, unknown>,
    successMessage: string,
    errorTitle: string,
  ) => {
    const normalized = normalizeIdentifier(serviceId);
    if (!normalized) return;
    setBusy(true);
    try {
      await adminSend("PUT", `/config/services/${encodeURIComponent(normalized)}`, updates, propertyCode);
      toast({ title: successMessage });
      await loadWorkspace(phaseId);
    } catch (error) {
      toast({
        title: errorTitle,
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  const deleteService = async (serviceId: string) => {
    const normalized = normalizeIdentifier(serviceId);
    if (!normalized) return;
    setBusy(true);
    try {
      await adminSend("DELETE", `/config/services/${encodeURIComponent(normalized)}`, undefined, propertyCode);
      toast({ title: "Service deleted" });
      if (editingServiceId && normalizeIdentifier(editingServiceId) === normalized) {
        resetEditor();
      }
      await loadWorkspace(phaseId);
    } catch (error) {
      toast({
        title: "Failed to delete service",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center py-8 text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading phase workspace...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="pt-6">
          <div className="grid gap-3 md:grid-cols-[1fr_auto]">
            <div className="space-y-1.5">
              <Label>Active Phase</Label>
              <Select value={phaseId} onValueChange={handlePhaseChange}>
                <SelectTrigger>
                  <SelectValue placeholder="Select phase" />
                </SelectTrigger>
                <SelectContent>
                  {phases.map((phase) => (
                    <SelectItem key={phase.id} value={phase.id}>
                      {phase.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                {selectedPhase?.description || "Select a phase to manage service mappings."}
              </p>
            </div>
            <div className="flex items-end">
              <Button variant="outline" onClick={() => loadWorkspace(phaseId)} disabled={busy}>
                {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCcw className="mr-2 h-4 w-4" />}
                Refresh
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Services In This Phase</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {phaseServices.length === 0 ? (
            <p className="text-sm text-muted-foreground">No services mapped to this phase yet.</p>
          ) : (
            phaseServices.map((service) => {
              const mode = asTicketingMode(
                service.ticketing_mode || (service.ticketing_enabled !== false ? "text" : "none"),
              );
              return (
                <div key={service.id} className="rounded-md border p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{service.name || service.id}</span>
                    <Badge variant="secondary">{service.type || "service"}</Badge>
                    <Badge variant="outline">{service.id}</Badge>
                    {service.is_builtin && <Badge>Prebuilt</Badge>}
                    <Badge variant={service.ticketing_enabled !== false ? "default" : "destructive"}>
                      {service.ticketing_enabled !== false ? "Ticketing On" : "Ticketing Off"}
                    </Badge>
                    <Badge variant="outline">Mode: {mode}</Badge>
                  </div>
                  {service.description && <p className="mt-2 text-sm text-muted-foreground">{service.description}</p>}
                  {service.ticketing_policy && (
                    <p className="mt-1 text-xs text-muted-foreground">Ticketing Rule: {service.ticketing_policy}</p>
                  )}
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <label className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={service.ticketing_enabled !== false}
                        onChange={(event) =>
                          void updateServiceToggle(
                            service.id,
                            { ticketing_enabled: event.target.checked },
                            `Ticketing ${event.target.checked ? "enabled" : "disabled"}`,
                            "Failed to update ticketing toggle",
                          )
                        }
                      />
                      Ticketing
                    </label>
                    <label className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={service.is_active !== false}
                        onChange={(event) =>
                          void updateServiceToggle(
                            service.id,
                            { is_active: event.target.checked },
                            `Service ${event.target.checked ? "activated" : "deactivated"}`,
                            "Failed to update service state",
                          )
                        }
                      />
                      Active
                    </label>
                    <Button variant="outline" size="sm" onClick={() => startEditing(service)}>
                      <Pencil className="mr-2 h-4 w-4" /> Edit
                    </Button>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => {
                        if (window.confirm("Delete this service completely?")) {
                          void deleteService(service.id);
                        }
                      }}
                    >
                      <Trash2 className="mr-2 h-4 w-4" /> Delete
                    </Button>
                  </div>
                </div>
              );
            })
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Add Existing Service To Phase</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Select value={selectedExistingServiceId} onValueChange={setSelectedExistingServiceId}>
            <SelectTrigger className="max-w-xl">
              <SelectValue placeholder="Select an existing service" />
            </SelectTrigger>
            <SelectContent>
              {existingCandidates.length === 0 ? (
                <SelectItem value="__none__" disabled>
                  All services are already mapped
                </SelectItem>
              ) : (
                existingCandidates.map((service) => (
                  <SelectItem key={service.id} value={service.id}>
                    {service.name || service.id} ({service.id})
                  </SelectItem>
                ))
              )}
            </SelectContent>
          </Select>
          <Button onClick={addExistingServiceToPhase} disabled={!selectedExistingServiceId || busy}>
            Add To Phase
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Prebuilt Services</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {prebuiltServices.length === 0 ? (
            <p className="text-sm text-muted-foreground">No prebuilt templates for this phase.</p>
          ) : (
            prebuiltServices.map((service) => (
              <div key={service.id} className="rounded-md border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{service.name || service.id}</span>
                  <Badge variant="secondary">{service.type || "service"}</Badge>
                  <Badge variant="outline">{service.id}</Badge>
                  {service.is_installed ? <Badge>Installed</Badge> : null}
                </div>
                {service.description && <p className="mt-2 text-sm text-muted-foreground">{service.description}</p>}
                <div className="mt-3">
                  <Button
                    size="sm"
                    onClick={() => void addPrebuiltServiceToPhase(service.id)}
                    disabled={service.is_installed === true || busy}
                  >
                    {service.is_installed ? "Added" : "Add"}
                  </Button>
                </div>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {isEditing ? `Edit Service (${editingServiceId})` : "Create New Service For This Phase"}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Service Name *</Label>
              <Input
                value={draft.name}
                onChange={(event) => {
                  const nextName = event.target.value;
                  updateDraft("name", nextName);
                  if (!isEditing && !isServiceIdManuallyEdited) {
                    updateDraft("id", normalizeIdentifier(makeServiceId(nextName).replace(/-/g, "_")));
                  }
                }}
                placeholder="e.g., Booking Modification Support"
              />
            </div>
            <div className="space-y-1.5">
              <Label>Service ID *</Label>
              <Input
                value={draft.id}
                disabled={isEditing}
                onChange={(event) => {
                  setIsServiceIdManuallyEdited(true);
                  updateDraft("id", normalizeIdentifier(event.target.value));
                }}
                placeholder="e.g., booking_modification_support"
                className="font-mono"
              />
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Service Type</Label>
              <Select value={draft.type} onValueChange={(value) => updateDraft("type", value)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="service">Service</SelectItem>
                  <SelectItem value="department">Department</SelectItem>
                  <SelectItem value="workflow">Workflow</SelectItem>
                  <SelectItem value="plugin">Plugin</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Ticketing Mode</Label>
              <Select
                value={draft.ticketingMode}
                onValueChange={(value) => updateDraft("ticketingMode", asTicketingMode(value))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">Disabled (no ticketing)</SelectItem>
                  <SelectItem value="text">Text mode</SelectItem>
                  <SelectItem value="form">Form mode</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>What do you expect from this service?</Label>
            <Textarea
              rows={2}
              value={draft.userIntent}
              onChange={(event) => updateDraft("userIntent", event.target.value)}
              placeholder="Describe expected behavior in plain language..."
            />
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label>Service Description *</Label>
              <Button variant="outline" size="sm" onClick={suggestDescription} disabled={busy}>
                <Sparkles className="mr-2 h-4 w-4" /> Refine with AI
              </Button>
            </div>
            <Textarea
              rows={2}
              value={draft.description}
              onChange={(event) => updateDraft("description", event.target.value)}
              placeholder="Description of what this service does."
            />
            {descriptionStatus && <p className="text-xs text-muted-foreground">{descriptionStatus}</p>}
          </div>

          {(draft.ticketingMode === "text" || draft.ticketingMode === "form") && (
            <div className="space-y-1.5 rounded-md border p-3">
              <div className="flex items-center justify-between">
                <Label>Ticketing Conditions</Label>
                <Button variant="outline" size="sm" onClick={suggestTicketingConditions} disabled={busy}>
                  <Sparkles className="mr-2 h-4 w-4" /> Refine with AI
                </Button>
              </div>
              <Textarea
                rows={2}
                value={draft.ticketingConditions}
                onChange={(event) => updateDraft("ticketingConditions", event.target.value)}
                placeholder="When exactly should ticket be raised for this service?"
              />
              {ticketingStatus && <p className="text-xs text-muted-foreground">{ticketingStatus}</p>}
            </div>
          )}

          {draft.ticketingMode === "form" ? (
            <div className="space-y-3 rounded-md border p-3">
              <div className="flex items-center justify-between">
                <Label>Form Builder</Label>
                <Button variant="outline" size="sm" onClick={() => setFormDraft(buildSuggestedFormDraft())}>
                  Auto Seed
                </Button>
              </div>
              <div className="grid gap-3 md:grid-cols-3">
                <Input
                  value={formDraft.triggerFieldId}
                  onChange={(event) => setFormField("triggerFieldId", normalizeIdentifier(event.target.value))}
                  placeholder="Trigger field id"
                />
                <Input
                  value={formDraft.triggerFieldLabel}
                  onChange={(event) => setFormField("triggerFieldLabel", event.target.value)}
                  placeholder="Trigger label"
                />
                <Input
                  value={formDraft.triggerFieldDescription}
                  onChange={(event) => setFormField("triggerFieldDescription", event.target.value)}
                  placeholder="Trigger description"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs font-semibold">Form Trigger Instructions</Label>
                <p className="text-xs text-muted-foreground">Tell the bot when to show the form. This gets baked into the service prompt on regeneration.</p>
                <Textarea
                  rows={3}
                  value={formDraft.preFormInstructions}
                  onChange={(event) => setFormField("preFormInstructions", event.target.value)}
                  placeholder='e.g. "Only trigger the form after the user explicitly confirms they want to book a specific room type. Asking about a room is NOT confirmation."'
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label>Fields</Label>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      setFormDraft((prev) => ({
                        ...prev,
                        fields: [...prev.fields, defaultField()],
                      }))
                    }
                  >
                    <Plus className="mr-2 h-4 w-4" /> Add Field
                  </Button>
                </div>
                {formDraft.fields.length === 0 ? <p className="text-xs text-muted-foreground">No fields yet.</p> : null}
                {formDraft.fields.map((field, index) => (
                  <div key={`field-${index}`} className="rounded-md border p-2">
                    <div className="grid gap-2 md:grid-cols-5">
                      <Input
                        value={field.id}
                        onChange={(event) => updateFormFieldDraft(index, "id", normalizeIdentifier(event.target.value))}
                        placeholder="Field ID"
                      />
                      <Input
                        value={field.label}
                        onChange={(event) => updateFormFieldDraft(index, "label", event.target.value)}
                        placeholder="Label"
                      />
                      <Select
                        value={field.type}
                        onValueChange={(value) => updateFormFieldDraft(index, "type", value as FormFieldType)}
                      >
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="text">Text</SelectItem>
                          <SelectItem value="date">Date</SelectItem>
                          <SelectItem value="time">Time</SelectItem>
                          <SelectItem value="number">Number</SelectItem>
                          <SelectItem value="tel">Phone</SelectItem>
                          <SelectItem value="email">Email</SelectItem>
                          <SelectItem value="textarea">Textarea</SelectItem>
                        </SelectContent>
                      </Select>
                      <label className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                        <input
                          type="checkbox"
                          checked={field.required}
                          onChange={(event) => updateFormFieldDraft(index, "required", event.target.checked)}
                        />
                        Required
                      </label>
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() =>
                          setFormDraft((prev) => ({
                            ...prev,
                            fields: prev.fields.filter((_, idx) => idx !== index),
                          }))
                        }
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                    <Input
                      className="mt-2"
                      value={field.validation_prompt}
                      onChange={(event) => updateFormFieldDraft(index, "validation_prompt", event.target.value)}
                      placeholder="Validation prompt (optional)"
                    />
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          <div className="space-y-3 rounded-md border p-3">
            <Label>Knowledge Input</Label>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                type="file"
                multiple
                accept=".pdf,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp"
                onChange={uploadMenus}
                className="max-w-lg"
              />
              <Button variant="outline" onClick={pullKbKnowledge} disabled={busy}>
                Pull from KB
              </Button>
            </div>
            {menuUploadStatus.length > 0 && (
              <div className="rounded-md border bg-muted/40 p-2 text-xs">
                {menuUploadStatus.map((row) => (
                  <p key={row}>{row}</p>
                ))}
              </div>
            )}
            <Textarea
              rows={6}
              value={draft.kbKnowledge}
              onChange={(event) => updateDraft("kbKnowledge", event.target.value)}
              placeholder="Upload menu files and/or pull KB context, then edit here."
            />
            {kbStatus && <p className="text-xs text-muted-foreground">{kbStatus}</p>}
          </div>

          <div className="space-y-2 rounded-md border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Label>Agent System Prompt</Label>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" onClick={regeneratePrompt} disabled={busy}>
                  Regenerate
                </Button>
                <Button variant="outline" size="sm" onClick={savePrompt} disabled={busy || !generatedPrompt.trim()}>
                  <Save className="mr-2 h-4 w-4" /> Save Prompt
                </Button>
              </div>
            </div>
            <Textarea
              rows={8}
              value={generatedPrompt}
              onChange={(event) => setGeneratedPrompt(event.target.value)}
              placeholder="Save service first, then regenerate or edit prompt manually."
              className="font-mono text-xs"
            />
            {promptStatus && <p className="text-xs text-muted-foreground">{promptStatus}</p>}
          </div>

          <div className="flex flex-wrap gap-2">
            <Button onClick={savePhaseService} disabled={busy}>
              {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
              {isEditing ? "Save Service" : "Create Service"}
            </Button>
            {isEditing ? (
              <Button variant="outline" onClick={resetEditor} disabled={busy}>
                Cancel Edit
              </Button>
            ) : null}
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

export default PhasesTab;
