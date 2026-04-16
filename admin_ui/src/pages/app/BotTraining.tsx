import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Download, Loader2, RotateCcw, Upload, Wand2 } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import IndustryStep from "@/components/bot-training/IndustryStep";
import BusinessInfoStep, { type BusinessInfo } from "@/components/bot-training/BusinessInfoStep";
import { type PromptData } from "@/components/bot-training/SystemPromptStep";
import { type KnowledgeData } from "@/components/bot-training/KnowledgeStep";
import ChannelsStep, { type ChannelsData } from "@/components/bot-training/ChannelsStep";
import FaqToolsTab from "@/components/bot-training/FaqToolsTab";
import ServicesTab from "@/components/bot-training/ServicesTab";
import PhasesTab from "@/components/bot-training/PhasesTab";
import RagAgentsTab from "@/components/bot-training/RagAgentsTab";
import EvaluationTab from "@/components/bot-training/EvaluationTab";
import EscalationTab from "@/components/bot-training/EscalationTab";
import AdvancedTab from "@/components/bot-training/AdvancedTab";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  adminGet,
  adminSend,
  getActivePropertyCode,
  joinLines,
  normalizePropertyCode,
  setActivePropertyCode,
  splitLines,
} from "@/lib/adminApi";

const industryDefaults: Record<string, { prompt: string; style: string; features: string }> = {
  hotel: {
    prompt:
      "You are {bot_name}, a virtual concierge for {business_name} in {city}. Help guests with room bookings, amenities, dining options, and local recommendations.",
    style: "professional, warm, helpful",
    features: "Room Booking\nCheck-in/Check-out\nRoom Service\nSpa Appointments\nLocal Recommendations",
  },
  restaurant: {
    prompt:
      "You are {bot_name}, the virtual host at {business_name} in {city}. Help guests with table reservations, menu inquiries, dietary accommodations, and special events.",
    style: "friendly, appetizing, conversational",
    features: "Table Reservations\nMenu Browsing\nDietary Info\nSpecial Events\nTakeaway Orders",
  },
  healthcare: {
    prompt:
      "You are {bot_name}, a virtual assistant for {business_name} in {city}. Help patients schedule appointments, find departments, and get general information. Never provide medical diagnosis.",
    style: "empathetic, clear, professional",
    features: "Appointment Booking\nDoctor Directory\nDepartment Info\nVisiting Hours\nEmergency Info",
  },
  spa: {
    prompt:
      "You are {bot_name}, a wellness assistant for {business_name} in {city}. Help guests book treatments, learn about services, and plan their wellness experience.",
    style: "calming, warm, inviting",
    features: "Treatment Booking\nService Menu\nMembership Info\nGift Vouchers\nTherapist Profiles",
  },
  automobile: {
    prompt:
      "You are {bot_name}, a virtual assistant for {business_name} in {city}. Help customers explore vehicles, schedule test drives, and get service information.",
    style: "knowledgeable, professional, enthusiastic",
    features: "Vehicle Catalog\nTest Drive Booking\nService Appointments\nSpare Parts Inquiry\nEMI Calculator",
  },
  retail: {
    prompt:
      "You are {bot_name}, a shopping assistant for {business_name} in {city}. Help customers find products, check availability, and track orders.",
    style: "helpful, upbeat, concise",
    features: "Product Search\nOrder Tracking\nStore Locator\nReturn/Exchange\nLoyalty Program",
  },
  travel: {
    prompt:
      "You are {bot_name}, a travel advisor for {business_name} in {city}. Help customers plan trips, book packages, and get travel information.",
    style: "adventurous, informative, friendly",
    features: "Trip Packages\nFlight Booking\nHotel Reservations\nVisa Assistance\nTravel Insurance",
  },
  events: {
    prompt:
      "You are {bot_name}, an event planning assistant for {business_name} in {city}. Help clients plan events, check availability, and coordinate services.",
    style: "organized, creative, enthusiastic",
    features: "Venue Availability\nEvent Packages\nCatering Options\nDecor Selection\nVendor Coordination",
  },
  banquet: {
    prompt:
      "You are {bot_name}, a banquet assistant for {business_name} in {city}. Help clients book halls, plan menus, and organize functions.",
    style: "professional, accommodating, detail-oriented",
    features: "Hall Booking\nMenu Planning\nSeating Arrangements\nDecor Packages\nAV Equipment",
  },
  education: {
    prompt:
      "You are {bot_name}, an academic assistant for {business_name} in {city}. Help students with admissions, course info, and campus services.",
    style: "informative, supportive, clear",
    features: "Course Catalog\nAdmission Inquiry\nFee Structure\nExam Schedule\nCampus Facilities",
  },
  realestate: {
    prompt:
      "You are {bot_name}, a property assistant for {business_name} in {city}. Help clients explore properties, schedule visits, and get pricing information.",
    style: "trustworthy, knowledgeable, persuasive",
    features: "Property Listings\nSite Visit Booking\nEMI Calculator\nFloor Plans\nLegal Documentation",
  },
  custom: {
    prompt:
      "You are {bot_name}, a virtual assistant for {business_name} in {city}. Help users with inquiries and provide accurate information about your services.",
    style: "professional, friendly, helpful",
    features: "General Inquiries\nAppointment Booking\nFAQ Support\nContact Info\nFeedback Collection",
  },
};

const defaultBusiness: BusinessInfo = {
  businessName: "",
  city: "",
  botName: "Nova",
  industryType: "hotel",
  currency: "INR",
  timezone: "Asia/Kolkata",
  language: "english",
  timestampFormat: "12h",
  location: "",
  contactEmail: "",
  contactPhone: "",
  website: "",
  address: "",
  welcomeMessage: "Hi! Welcome to {business_name}. How can I help you today?",
};

const defaultPrompt: PromptData = {
  promptTemplate: "default",
  responseStyle: "professional, friendly",
  systemPrompt: "",
  classifierPrompt: "",
};

const defaultKnowledge: KnowledgeData = {
  knowledgeSources: "",
  nluDoRules: "",
  nluDontRules: "",
  knowledgeNotes: "",
};

const defaultChannels: ChannelsData = {
  primaryColor: "#C72C41",
  accentColor: "#C72C41",
  bgColor: "#FFFFFF",
  textColor: "#1A1A2E",
  widgetPosition: "right",
  widgetWidth: 380,
  widgetHeight: 600,
  industryFeatures: "",
  webEnabled: true,
  whatsappEnabled: false,
};

const legacyTabAliases: Record<string, string> = {
  "faq-tools": "faq",
  "rag-agents": "rag",
};

const tabItems = [
  { value: "wizard", label: "Setup Wizard" },
  { value: "rag", label: "RAG" },
  { value: "phases", label: "Phases" },
  { value: "services", label: "Services" },
  { value: "faq", label: "FAQ" },
  { value: "evaluation", label: "Evaluation" },
  { value: "escalation", label: "Escalation" },
  { value: "advanced", label: "Advanced" },
];

interface PropertyOption {
  code: string;
  name: string;
  city: string;
}

const languageToUi: Record<string, string> = {
  en: "english",
  hi: "hindi",
  es: "spanish",
  fr: "french",
  ar: "arabic",
  english: "english",
  hindi: "hindi",
  spanish: "spanish",
  french: "french",
  arabic: "arabic",
};

const languageToApi: Record<string, string> = {
  english: "en",
  hindi: "hi",
  spanish: "es",
  french: "fr",
  arabic: "ar",
};

const BotTraining = () => {
  const { toast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const rawTab = String(searchParams.get("tab") || "wizard");
  const resolvedTab = legacyTabAliases[rawTab] || rawTab;
  const activeTab = tabItems.some((tab) => tab.value === resolvedTab) ? resolvedTab : "wizard";

  const [industry, setIndustry] = useState("hotel");
  const [business, setBusiness] = useState<BusinessInfo>(defaultBusiness);
  const [prompt, setPrompt] = useState<PromptData>(defaultPrompt);
  const [knowledge, setKnowledge] = useState<KnowledgeData>(defaultKnowledge);
  const [channels, setChannels] = useState<ChannelsData>(defaultChannels);
  const [propertyCode, setPropertyCode] = useState(getActivePropertyCode());
  const [propertyCodeManuallyEdited, setPropertyCodeManuallyEdited] = useState(true);
  const [propertyCodes, setPropertyCodes] = useState<string[]>([]);
  const [propertyOptions, setPropertyOptions] = useState<PropertyOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const displayIndustry = useMemo(() => industry || business.industryType || "hotel", [industry, business.industryType]);

  const loadPropertyCodes = useCallback(async (currentCode: string) => {
    try {
      const payload = await adminGet<{ properties?: Array<{ id?: string; code?: string; name?: string; city?: string }> }>(
        "/properties",
        currentCode,
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

      const mergedByCode = new Map<string, PropertyOption>();
      for (const row of rows) mergedByCode.set(row.code, row);
      if (currentCode && !mergedByCode.has(currentCode)) {
        mergedByCode.set(currentCode, { code: currentCode, name: currentCode, city: "" });
      }
      const mergedRows = [...mergedByCode.values()].sort((a, b) => a.code.localeCompare(b.code));
      setPropertyOptions(mergedRows);
      setPropertyCodes(mergedRows.map((row) => row.code));
    } catch {
      setPropertyOptions([{ code: currentCode, name: currentCode, city: "" }]);
      setPropertyCodes([currentCode]);
    }
  }, []);

  const loadStateForProperty = useCallback(
    async (rawCode: string) => {
      const code = setActivePropertyCode(rawCode || "default");
      setPropertyCode(code);
      setPropertyCodeManuallyEdited(true);
      setLoading(true);
      try {
        await loadPropertyCodes(code);
        const [businessPayload, promptPayload, knowledgePayload, uiPayload] = await Promise.all([
          adminGet<Record<string, unknown>>("/config/onboarding/business", code),
          adminGet<Record<string, unknown>>("/config/onboarding/prompts", code),
          adminGet<Record<string, unknown>>("/config/onboarding/knowledge", code),
          adminGet<Record<string, unknown>>("/config/onboarding/ui", code),
        ]);

        const businessType = String(businessPayload?.type || "hotel");
        const nextIndustry = businessType || "hotel";
        setIndustry(nextIndustry);

        setBusiness({
          businessName: String(businessPayload?.name || ""),
          city: String(businessPayload?.city || ""),
          botName: String(businessPayload?.bot_name || "Nova"),
          industryType: nextIndustry,
          currency: String(businessPayload?.currency || "INR"),
          timezone: String(businessPayload?.timezone || "Asia/Kolkata"),
          language: languageToUi[String(businessPayload?.language || "en").toLowerCase()] || "english",
          timestampFormat: String(businessPayload?.timestamp_format || "12h").toLowerCase() === "24h" ? "24h" : "12h",
          location: String(businessPayload?.location || ""),
          contactEmail: String(businessPayload?.contact_email || ""),
          contactPhone: String(businessPayload?.contact_phone || ""),
          website: String(businessPayload?.website || ""),
          address: String(businessPayload?.address || ""),
          welcomeMessage:
            String(businessPayload?.welcome_message || "").trim() || defaultBusiness.welcomeMessage,
        });

        setPrompt({
          promptTemplate: String(promptPayload?.template_id || "default"),
          responseStyle: String(promptPayload?.response_style || ""),
          systemPrompt: String(promptPayload?.system_prompt || ""),
          classifierPrompt: String(promptPayload?.classifier_prompt || ""),
        });

        const nluPolicy = (knowledgePayload?.nlu_policy || {}) as Record<string, unknown>;
        setKnowledge({
          knowledgeSources: joinLines(knowledgePayload?.sources),
          nluDoRules: joinLines(nluPolicy?.dos),
          nluDontRules: joinLines(nluPolicy?.donts),
          knowledgeNotes: String(knowledgePayload?.notes || ""),
        });

        const uiTheme = (uiPayload?.theme || {}) as Record<string, unknown>;
        const uiWidget = (uiPayload?.widget || {}) as Record<string, unknown>;
        const uiChannels = (uiPayload?.channels || {}) as Record<string, unknown>;
        const channelsFromBusiness = (businessPayload?.channels || {}) as Record<string, unknown>;
        setChannels((prev) => ({
          ...prev,
          primaryColor: String(uiTheme?.primary_color || prev.primaryColor),
          accentColor: String(uiTheme?.accent_color || prev.accentColor),
          bgColor: String(uiTheme?.background_color || prev.bgColor),
          textColor: String(uiTheme?.text_color || prev.textColor),
          widgetPosition: String(uiWidget?.position || prev.widgetPosition),
          widgetWidth: Number(uiWidget?.width || prev.widgetWidth) || prev.widgetWidth,
          widgetHeight: Number(uiWidget?.height || prev.widgetHeight) || prev.widgetHeight,
          industryFeatures:
            joinLines(uiPayload?.industry_features) ||
            industryDefaults[nextIndustry]?.features ||
            prev.industryFeatures,
          webEnabled:
            typeof (uiChannels?.web_widget as { enabled?: boolean })?.enabled === "boolean"
              ? Boolean((uiChannels?.web_widget as { enabled?: boolean }).enabled)
              : Boolean(channelsFromBusiness?.web_widget ?? prev.webEnabled),
          whatsappEnabled:
            typeof (uiChannels?.whatsapp as { enabled?: boolean })?.enabled === "boolean"
              ? Boolean((uiChannels?.whatsapp as { enabled?: boolean }).enabled)
              : Boolean(channelsFromBusiness?.whatsapp ?? prev.whatsappEnabled),
        }));
      } catch (error) {
        toast({
          title: "Failed to load admin data",
          description: String(error instanceof Error ? error.message : error),
          variant: "destructive",
        });
      } finally {
        setLoading(false);
      }
    },
    [loadPropertyCodes, toast],
  );

  useEffect(() => {
    loadStateForProperty(getActivePropertyCode());
  }, [loadStateForProperty]);

  useEffect(() => {
    if (activeTab === rawTab) return;
    setSearchParams(activeTab === "wizard" ? {} : { tab: activeTab }, { replace: true });
  }, [activeTab, rawTab, setSearchParams]);

  const handleTabChange = (value: string) => {
    setSearchParams(value === "wizard" ? {} : { tab: value });
  };

  const handleIndustrySelect = useCallback((value: string) => {
    setIndustry(value);
    setBusiness((prev) => ({ ...prev, industryType: value }));
    const defaults = industryDefaults[value];
    if (defaults) {
      setChannels((prev) => ({ ...prev, industryFeatures: defaults.features }));
      if (!prompt.systemPrompt.trim()) {
        setPrompt((prev) => ({ ...prev, systemPrompt: defaults.prompt, responseStyle: defaults.style }));
      }
    }
  }, [prompt.systemPrompt]);

  const handleBusinessChange = useCallback((field: keyof BusinessInfo, value: string) => {
    setBusiness((prev) => ({ ...prev, [field]: value }));
    if (field === "businessName") {
      const shouldAutoFillPropertyCode = !propertyCodeManuallyEdited || !String(propertyCode || "").trim();
      if (shouldAutoFillPropertyCode) {
        setPropertyCode(normalizePropertyCode(value));
      }
    }
  }, [propertyCode, propertyCodeManuallyEdited]);

  const handlePromptChange = useCallback((field: keyof PromptData, value: string) => {
    setPrompt((prev) => ({ ...prev, [field]: value }));
  }, []);

  const handleKnowledgeChange = useCallback((field: keyof KnowledgeData, value: string) => {
    setKnowledge((prev) => ({ ...prev, [field]: value }));
  }, []);

  const handleChannelsChange = useCallback(<K extends keyof ChannelsData>(field: K, value: ChannelsData[K]) => {
    setChannels((prev) => ({ ...prev, [field]: value }));
  }, []);

  const saveBusiness = useCallback(async () => {
    const normalizedCode = normalizePropertyCode(propertyCode);
    if (!normalizedCode || !business.businessName.trim() || !business.city.trim() || !business.botName.trim()) {
      toast({
        title: "Missing required fields",
        description: "Property code, business name, city, and bot name are required.",
        variant: "destructive",
      });
      return;
    }
    setSaving(true);
    try {
      setActivePropertyCode(normalizedCode);
      await adminSend(
        "PUT",
        "/config/onboarding/business",
        {
          id: normalizedCode,
          name: business.businessName.trim(),
          type: displayIndustry,
          city: business.city.trim(),
          bot_name: business.botName.trim(),
          currency: business.currency,
          timezone: business.timezone,
          language: languageToApi[business.language] || "en",
          timestamp_format: business.timestampFormat,
          location: business.location.trim(),
          address: business.address.trim(),
          contact_email: business.contactEmail.trim(),
          contact_phone: business.contactPhone.trim(),
          website: business.website.trim(),
          channels: {
            web_widget: channels.webEnabled,
            whatsapp: channels.whatsappEnabled,
          },
          welcome_message: business.welcomeMessage.trim(),
        },
        normalizedCode,
      );
      await loadPropertyCodes(normalizedCode);
      toast({ title: "Business info saved" });
    } catch (error) {
      toast({
        title: "Failed to save business",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  }, [propertyCode, business, channels, displayIndustry, toast, loadPropertyCodes]);

  const savePrompt = useCallback(async () => {
    try {
      await adminSend(
        "PUT",
        "/config/onboarding/prompts",
        {
          template_id: prompt.promptTemplate || null,
          system_prompt: prompt.systemPrompt,
          classifier_prompt: prompt.classifierPrompt,
          response_style: prompt.responseStyle,
        },
        propertyCode,
      );
      toast({ title: "Prompt settings saved" });
    } catch (error) {
      toast({
        title: "Failed to save prompt settings",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    }
  }, [prompt, propertyCode, toast]);

  const saveKnowledge = useCallback(async () => {
    try {
      await adminSend(
        "PUT",
        "/config/onboarding/knowledge",
        {
          sources: splitLines(knowledge.knowledgeSources),
          notes: knowledge.knowledgeNotes,
          nlu_policy: {
            dos: splitLines(knowledge.nluDoRules),
            donts: splitLines(knowledge.nluDontRules),
          },
        },
        propertyCode,
      );
      toast({ title: "Knowledge settings saved" });
    } catch (error) {
      toast({
        title: "Failed to save knowledge",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    }
  }, [knowledge, propertyCode, toast]);

  const saveChannels = useCallback(async () => {
    try {
      await adminSend(
        "PUT",
        "/config/onboarding/ui",
        {
          theme: {
            primary_color: channels.primaryColor,
            accent_color: channels.accentColor,
            background_color: channels.bgColor,
            text_color: channels.textColor,
          },
          widget: {
            position: channels.widgetPosition,
            width: channels.widgetWidth,
            height: channels.widgetHeight,
          },
          channels: {
            web_widget: { enabled: channels.webEnabled },
            whatsapp: { enabled: channels.whatsappEnabled },
          },
          industry_features: splitLines(channels.industryFeatures),
        },
        propertyCode,
      );
      toast({ title: "Channel settings saved" });
    } catch (error) {
      toast({
        title: "Failed to save channel settings",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    }
  }, [channels, propertyCode, toast]);

  const applyIndustryTemplate = useCallback(async () => {
    const defaults = industryDefaults[displayIndustry];
    if (!defaults) {
      toast({ title: "Select an industry first", variant: "destructive" });
      return;
    }
    try {
      await adminSend(
        "POST",
        "/config/templates/apply",
        {
          template_name: displayIndustry,
          business_id: normalizePropertyCode(propertyCode) || "default",
          business_name: business.businessName || "My Business",
          city: business.city || "City",
          bot_name: business.botName || "Assistant",
        },
        propertyCode,
      );
      await loadStateForProperty(propertyCode);
      toast({ title: "Industry template applied" });
    } catch (error) {
      toast({
        title: "Failed to apply industry template",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    }
  }, [displayIndustry, propertyCode, business.businessName, business.city, business.botName, loadStateForProperty, toast]);

  const handleApplyPromptTemplate = useCallback(async () => {
    if (!prompt.promptTemplate) return;
    try {
      await adminSend(
        "POST",
        "/config/onboarding/prompts/apply-template",
        { template_id: prompt.promptTemplate },
        propertyCode,
      );
      await loadStateForProperty(propertyCode);
      toast({ title: "Prompt template applied" });
    } catch (error) {
      toast({
        title: "Failed to apply prompt template",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    }
  }, [prompt.promptTemplate, propertyCode, loadStateForProperty, toast]);

  const exportConfig = useCallback(async () => {
    try {
      const data = await adminGet<{ config_json?: string }>("/config/export", propertyCode);
      const raw = String(data?.config_json || "").trim();
      if (!raw) throw new Error("Config export is empty");
      const blob = new Blob([raw], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "bot_config.json";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
      toast({ title: "Config exported" });
    } catch (error) {
      toast({
        title: "Failed to export config",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    }
  }, [propertyCode, toast]);

  const importConfig = useCallback(() => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json,application/json";
    input.onchange = async (event) => {
      const file = (event.target as HTMLInputElement).files?.[0];
      if (!file) return;
      const text = await file.text();
      try {
        JSON.parse(text);
        await adminSend("POST", "/config/import", { config_json: text }, propertyCode);
        await loadStateForProperty(propertyCode);
        toast({ title: "Config imported" });
      } catch (error) {
        toast({
          title: "Failed to import config",
          description: String(error instanceof Error ? error.message : error),
          variant: "destructive",
        });
      }
    };
    input.click();
  }, [propertyCode, loadStateForProperty, toast]);

  const resetWizard = useCallback(async () => {
    setBusiness(defaultBusiness);
    setPrompt(defaultPrompt);
    setKnowledge(defaultKnowledge);
    setChannels(defaultChannels);
    setIndustry("hotel");
    toast({ title: "Wizard reset locally", description: "Click Save on each step to persist." });
  }, [toast]);

  if (loading) {
    return (
      <div className="mx-auto flex max-w-5xl items-center justify-center py-24 text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading bot training settings...
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Bot Training & Setup</h1>
        <p className="text-muted-foreground">
          Configure your bot setup, RAG, phases, services, FAQ, evaluation, escalation, and advanced settings.
        </p>
      </div>

      <div className="grid gap-3 rounded-lg border bg-card p-4">
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="property-picker">Saved Properties</Label>
            <select
              id="property-picker"
              className="h-10 w-full rounded-md border bg-background px-3 text-sm"
              value={propertyOptions.some((row) => row.code === propertyCode) ? propertyCode : ""}
              onChange={(event) => {
                const raw = event.target.value;
                if (raw === "__new__") {
                  setPropertyCode("");
                  setPropertyCodeManuallyEdited(false);
                  setBusiness(defaultBusiness);
                  setPrompt(defaultPrompt);
                  setKnowledge(defaultKnowledge);
                  setChannels(defaultChannels);
                  setIndustry("hotel");
                  toast({
                    title: "New property",
                    description: "Fields cleared. Enter a property code and fill details, then Save Business.",
                  });
                  return;
                }
                const next = normalizePropertyCode(raw);
                if (next) {
                  setPropertyCode(next);
                  setPropertyCodeManuallyEdited(true);
                }
              }}
            >
              <option value="">Select saved property...</option>
              <option value="__new__">+ Create New Property</option>
              {propertyOptions.map((row) => (
                <option key={row.code} value={row.code}>
                  {[row.code, row.name, row.city].filter(Boolean).join(" | ")}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="property-code">Property Code</Label>
            <Input
              id="property-code"
              value={propertyCode}
              onChange={(e) => {
                setPropertyCodeManuallyEdited(true);
                setPropertyCode(normalizePropertyCode(e.target.value));
              }}
              list="property-code-list"
              placeholder="e.g. test or khil10"
            />
            <datalist id="property-code-list">
              {propertyCodes.map((code) => (
                <option key={code} value={code} />
              ))}
            </datalist>
          </div>
        </div>
        <div className="flex items-end gap-2">
          <Button variant="outline" onClick={() => loadStateForProperty(propertyCode)} disabled={saving}>
            Load Property
          </Button>
          <Button onClick={saveBusiness} disabled={saving}>
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            Save Business
          </Button>
        </div>
      </div>

      <Tabs value={activeTab} onValueChange={handleTabChange}>
        <TabsList className="flex h-auto flex-wrap gap-1">
          {tabItems.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value} className="text-xs sm:text-sm">
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="wizard" className="mt-4 space-y-6">
          <IndustryStep selected={displayIndustry} onSelect={handleIndustrySelect} onSave={saveBusiness} />
          <BusinessInfoStep data={business} onChange={handleBusinessChange} onSave={saveBusiness} />
          <ChannelsStep
            data={channels}
            propertyCode={propertyCode}
            botName={business.botName}
            onChange={handleChannelsChange}
            onSave={saveChannels}
          />

          <div className="flex flex-wrap gap-3 rounded-lg border bg-card p-4">
            <Button variant="outline" onClick={applyIndustryTemplate}>
              <Wand2 className="mr-2 h-4 w-4" /> Apply Industry Template
            </Button>
            <Button variant="outline" onClick={exportConfig}>
              <Download className="mr-2 h-4 w-4" /> Export Config
            </Button>
            <Button variant="outline" onClick={importConfig}>
              <Upload className="mr-2 h-4 w-4" /> Import Config
            </Button>
            <Button variant="outline" onClick={resetWizard}>
              <RotateCcw className="mr-2 h-4 w-4" /> Reset Wizard
            </Button>
          </div>
        </TabsContent>

        <TabsContent value="rag">
          <RagAgentsTab
            propertyCode={propertyCode}
            businessType={displayIndustry}
            businessName={business.businessName}
            city={business.city}
          />
        </TabsContent>
        <TabsContent value="phases">
          <PhasesTab propertyCode={propertyCode} />
        </TabsContent>
        <TabsContent value="services">
          <ServicesTab propertyCode={propertyCode} />
        </TabsContent>
        <TabsContent value="faq">
          <FaqToolsTab propertyCode={propertyCode} />
        </TabsContent>
        <TabsContent value="evaluation">
          <EvaluationTab propertyCode={propertyCode} />
        </TabsContent>
        <TabsContent value="escalation">
          <EscalationTab propertyCode={propertyCode} />
        </TabsContent>
        <TabsContent value="advanced">
          <AdvancedTab propertyCode={propertyCode} onImported={() => loadStateForProperty(propertyCode)} />
        </TabsContent>
      </Tabs>
    </div>
  );
};

export default BotTraining;
