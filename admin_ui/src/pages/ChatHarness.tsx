import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { MessageCircle, Maximize2, Minus, Plus, Send, X } from "lucide-react";
import { useIsMobile } from "@/hooks/use-mobile";

type Role = "user" | "assistant";

const WIDGET_BRAND_COLOR_DEFAULT = "#C72C41";
const WIDGET_ACCENT_COLOR_DEFAULT = "#C72C41";
const WIDGET_BACKGROUND_COLOR_DEFAULT = "#FFFFFF";
const WIDGET_TEXT_COLOR_DEFAULT = "#1A1A2E";
const WIDGET_BOT_NAME_DEFAULT = "Kebo";
const WIDGET_DEFAULT_WIDTH = 380;
const WIDGET_DEFAULT_HEIGHT = 620;
const WIDGET_EXPANDED_WIDTH = 520;
const WIDGET_EXPANDED_HEIGHT = 760;

interface PropertyOption {
  code: string;
  name: string;
  city: string;
}

interface PhaseOption {
  id: string;
  name: string;
}

interface ProfileMap {
  guest_id?: string;
  entity_id?: string;
  organisation_id?: string;
  room_number?: string;
  guest_phone?: string;
  guest_name?: string;
  group_id?: string;
  ticket_source?: string;
  flow?: string;
}

interface BookingRow {
  booking_id: number;
  guest_id?: number;
  confirmation_code?: string;
  property_name?: string;
  room_number?: string;
  room_type?: string;
  check_in_date?: string;
  check_out_date?: string;
  guest_name?: string;
  guest_phone?: string;
  status?: string;
  phase?: string;
}

interface ChatMessageRow {
  id: string;
  role: Role;
  content: string;
  canonicalContent: string;
  raw?: ChatApiResponse;
}

interface ChatApiResponse {
  session_id?: string;
  message?: string;
  display_message?: string;
  state?: string;
  suggested_actions?: string[];
  service_llm_label?: string;
  metadata?: Record<string, unknown>;
  form_fields?: Array<Record<string, unknown>>;
  form_service_id?: string;
}

interface SuggestionsApiResponse {
  suggestions?: string[];
  prefetch_batch_id?: string | null;
}

interface InlineFormField {
  id: string;
  label: string;
  type: string;
  required: boolean;
  options: string[];
  placeholder: string;
}

interface InlineFormState {
  messageId: string;
  serviceId: string;
  fields: InlineFormField[];
  values: Record<string, string>;
  countryCodes: Record<string, string>;
  errors: Record<string, string>;
  submitting: boolean;
  successMessage: string;
}

interface TicketStatus {
  label: string;
  badge: "idle" | "created" | "not-created" | "pending" | "failed";
}

interface SessionHistory {
  session_id: string;
  state: string;
  hotel_code: string;
  created_at: string;
  messages: Array<{ role: string; content: string; timestamp: string }>;
}

interface TicketRow {
  ticket_id?: string;
  id?: string;
  status?: string;
  created_at?: string;
  [key: string]: unknown;
}

const PHASE_FALLBACK: PhaseOption[] = [
  { id: "pre_booking", name: "Pre Booking" },
  { id: "pre_checkin", name: "Pre Checkin" },
  { id: "during_stay", name: "During Stay" },
  { id: "post_checkout", name: "Post Checkout" },
];

const PHONE_CODES = ["+91", "+1", "+44", "+971", "+65", "+61", "+49", "+33", "+81", "+86"];

function generateSessionId() {
  return `session_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

function normalizePhaseId(value: unknown): string {
  const raw = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/-/g, "_")
    .replace(/\s+/g, "_");
  if (!raw) return "";
  const aliases: Record<string, string> = {
    prebooking: "pre_booking",
    booking: "pre_checkin",
    precheckin: "pre_checkin",
    duringstay: "during_stay",
    instay: "during_stay",
    in_stay: "during_stay",
    postcheckout: "post_checkout",
  };
  return aliases[raw] || raw;
}

function resolveAssistantDisplayMessage(data: ChatApiResponse): string {
  const display = String(data.display_message || "").trim();
  if (display) return display;
  const metadataDisplay = String((data.metadata || {}).display_message || "").trim();
  if (metadataDisplay) return metadataDisplay;
  return String(data.message || "").trim();
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function normalizeSafeLink(url: string): string {
  const raw = String(url || "").trim();
  if (!raw) return "";
  const withProtocol = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
  try {
    const parsed = new URL(withProtocol);
    if (!["http:", "https:"].includes(parsed.protocol)) return "";
    return parsed.href;
  } catch {
    return "";
  }
}

function buildAssistantLinkHtml(url: string, label: string): string {
  const href = normalizeSafeLink(url);
  const text = String(label || url || "").trim();
  if (!href || !text) return escapeHtml(text || url || "");
  return `<a class="underline underline-offset-2 text-teal-700 hover:text-teal-800" href="${escapeHtml(
    href,
  )}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`;
}

function renderAssistantMarkdownToHtml(text: string): string {
  const escaped = escapeHtml(String(text || ""));
  if (!escaped) return "";
  let rendered = escaped;
  rendered = rendered.replace(/\*\*([\s\S]+?)\*\*/g, "<strong>$1</strong>");
  rendered = rendered.replace(/__([\s\S]+?)__/g, "<strong>$1</strong>");
  rendered = rendered.replace(/(?<!\w)\*((?!\s)[^*]+(?<!\s))\*(?!\w)/g, "<em>$1</em>");
  rendered = rendered.replace(/(?<!\w)_((?!\s)[^_]+(?<!\s))_(?!\w)/g, "<em>$1</em>");
  rendered = rendered.replace(/^#{1,3}\s+(.+)$/gm, "<strong>$1</strong>");
  rendered = rendered.replace(/\[([^\]\n]+?)\]\((https?:\/\/[^\s)]+)\)/g, (_match, label, url) =>
    buildAssistantLinkHtml(url, label),
  );
  rendered = rendered.replace(/(^|[\s(>])((?:https?:\/\/|www\.)[^\s<]+)/gi, (_match, prefix, url) => {
    const trailing = (url.match(/[).,!?:;]+$/) || [""])[0];
    const cleanUrl = trailing ? url.slice(0, -trailing.length) : url;
    const linked = buildAssistantLinkHtml(cleanUrl, cleanUrl);
    return `${prefix}${linked}${trailing}`;
  });
  return rendered;
}

function formatWelcomeMessage(
  template: string,
  context: { bot_name: string; business_name: string; city: string },
): string {
  const fallback = "Hi! How can I help you today?";
  const raw = String(template || "").trim() || fallback;
  return raw
    .replace(/\{bot_name\}/gi, context.bot_name || "Assistant")
    .replace(/\{business_name\}/gi, context.business_name || "your property")
    .replace(/\{city\}/gi, context.city || "");
}

function normalizeFormFields(raw: unknown): InlineFormField[] {
  if (!Array.isArray(raw)) return [];
  const rows: InlineFormField[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const id = String(row.id || "").trim();
    const label = String(row.label || id || "").trim();
    if (!id || !label) continue;
    const options = Array.isArray(row.options)
      ? row.options.map((entry) => String(entry || "").trim()).filter(Boolean)
      : Array.isArray(row.choices)
        ? row.choices.map((entry) => String(entry || "").trim()).filter(Boolean)
        : [];
    rows.push({
      id,
      label,
      type: String(row.type || "text").trim().toLowerCase().replace(/_/g, "-") || "text",
      required: Boolean(row.required),
      options,
      placeholder: String(row.placeholder || "").trim(),
    });
  }
  return rows;
}

function isPhoneField(field: InlineFormField): boolean {
  if (field.type === "tel") return true;
  const hint = `${field.id} ${field.label}`.toLowerCase();
  return /\b(phone|mobile|contact|cell|whatsapp)\b/.test(hint);
}

function getStayDateBounds(phase: string, booking: BookingRow | null): { min: string; max: string } {
  const todayStr = new Date().toISOString().slice(0, 10);
  const normalized = normalizePhaseId(phase);
  const checkIn = String(booking?.check_in_date || "");
  const checkOut = String(booking?.check_out_date || "");
  if (normalized === "pre_checkin" && checkIn && checkOut) {
    const min = checkIn > todayStr ? checkIn : todayStr;
    return { min, max: checkOut };
  }
  if (normalized === "during_stay" && checkOut) {
    return { min: todayStr, max: checkOut };
  }
  return { min: todayStr, max: "" };
}

interface EmbedRuntimeConfig {
  embedMode: boolean;
  widgetId: string;
  hotelCode: string;
  phase: string;
  sessionId: string;
  brandColor: string;
  accentColor: string;
  backgroundColor: string;
  textColor: string;
  botName: string;
  position: "left" | "right";
  width: number;
  height: number;
}

function parseBooleanParam(value: string | null, fallback = false): boolean {
  const raw = String(value || "")
    .trim()
    .toLowerCase();
  if (!raw) return fallback;
  if (["1", "true", "yes", "on"].includes(raw)) return true;
  if (["0", "false", "no", "off"].includes(raw)) return false;
  return fallback;
}

function parseNumberParam(value: string | null, fallback: number, min: number, max: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.round(parsed)));
}

function normalizeColorParam(raw: string, fallback: string): string {
  const value = String(raw || "").trim();
  if (!value) return fallback;
  const withHash = value.startsWith("#") ? value : `#${value}`;
  return /^#[0-9a-fA-F]{3,8}$/.test(withHash) ? withHash : fallback;
}

function readEmbedRuntimeConfig(): EmbedRuntimeConfig {
  if (typeof window === "undefined") {
    return {
      embedMode: false,
      widgetId: "default",
      hotelCode: "default",
      phase: "pre_booking",
      sessionId: generateSessionId(),
      brandColor: WIDGET_BRAND_COLOR_DEFAULT,
      accentColor: WIDGET_ACCENT_COLOR_DEFAULT,
      backgroundColor: WIDGET_BACKGROUND_COLOR_DEFAULT,
      textColor: WIDGET_TEXT_COLOR_DEFAULT,
      botName: WIDGET_BOT_NAME_DEFAULT,
      position: "right",
      width: WIDGET_DEFAULT_WIDTH,
      height: WIDGET_DEFAULT_HEIGHT,
    };
  }

  const params = new URLSearchParams(window.location.search);
  const embedMode = parseBooleanParam(params.get("embed"), false);
  const hotelCode = String(params.get("hotel_code") || "default")
    .trim()
    .toLowerCase() || "default";
  const phase = normalizePhaseId(params.get("phase") || "pre_booking") || "pre_booking";
  const position = String(params.get("position") || "")
    .trim()
    .toLowerCase() === "left"
    ? "left"
    : "right";
  const width = parseNumberParam(params.get("width"), WIDGET_DEFAULT_WIDTH, 280, 600);
  const height = parseNumberParam(params.get("height"), WIDGET_DEFAULT_HEIGHT, 360, 900);
  const sessionId = String(params.get("session_id") || "").trim() || generateSessionId();
  const widgetId = String(params.get("widget_id") || "default").trim() || "default";
  const brandColor = normalizeColorParam(params.get("brand_color") || WIDGET_BRAND_COLOR_DEFAULT, WIDGET_BRAND_COLOR_DEFAULT);
  const accentColor = normalizeColorParam(
    params.get("accent_color") || params.get("brand_color") || WIDGET_ACCENT_COLOR_DEFAULT,
    WIDGET_ACCENT_COLOR_DEFAULT,
  );
  const backgroundColor = normalizeColorParam(
    params.get("bg_color") || WIDGET_BACKGROUND_COLOR_DEFAULT,
    WIDGET_BACKGROUND_COLOR_DEFAULT,
  );
  const textColor = normalizeColorParam(params.get("text_color") || WIDGET_TEXT_COLOR_DEFAULT, WIDGET_TEXT_COLOR_DEFAULT);
  const botName = String(params.get("bot_name") || WIDGET_BOT_NAME_DEFAULT).trim() || WIDGET_BOT_NAME_DEFAULT;

  return {
    embedMode,
    widgetId,
    hotelCode,
    phase,
    sessionId,
    brandColor,
    accentColor,
    backgroundColor,
    textColor,
    botName,
    position,
    width,
    height,
  };
}

function resolveTicketStatus(metadata: Record<string, unknown>): TicketStatus {
  const ticketId = String(metadata.ticket_id || "").trim();
  const ticketState = String(metadata.ticket_status || "")
    .trim()
    .toLowerCase();
  const ticketError = String(metadata.ticket_create_error || "").trim();
  const skipReason = String(
    metadata.ticket_create_skip_reason || metadata.ticket_skip_reason || "",
  ).trim();

  if (metadata.ticket_created === true || ticketId) {
    const stateSuffix = ticketState ? ` (${ticketState})` : "";
    return {
      label: `Created: ${ticketId || "unknown-id"}${stateSuffix}`,
      badge: "created",
    };
  }
  if (ticketError) {
    return {
      label: `Not created: ${ticketError}`,
      badge: "failed",
    };
  }
  if (metadata.ticket_created === false || skipReason) {
    return {
      label: skipReason ? `Not created: ${skipReason}` : "Not created",
      badge: "not-created",
    };
  }
  if (metadata.ticketing_required === true && metadata.ticketing_create_allowed === false) {
    return { label: "Not created: gated", badge: "not-created" };
  }
  if (metadata.ticketing_required === true) {
    return { label: "Ticket required", badge: "pending" };
  }
  return { label: "No ticket action", badge: "idle" };
}

function resolveTicketDetails(metadata: Record<string, unknown>): Record<string, unknown> | null {
  const ticketId = String(metadata.ticket_id || "").trim();
  const created = metadata.ticket_created === true || Boolean(ticketId);
  if (!created) return null;

  const apiResponse = metadata.ticket_api_response;
  if (apiResponse && typeof apiResponse === "object") {
    const payload = apiResponse as Record<string, unknown>;
    const rawRecord = payload.ticket_record || payload.record || payload.ticket;
    if (rawRecord && typeof rawRecord === "object") {
      return rawRecord as Record<string, unknown>;
    }
  }

  if (metadata.ticket_record && typeof metadata.ticket_record === "object") {
    return metadata.ticket_record as Record<string, unknown>;
  }

  const fallback: Record<string, unknown> = {};
  const keys = [
    "ticket_id",
    "ticket_status",
    "ticket_category",
    "ticket_sub_category",
    "ticket_priority",
    "ticket_summary",
    "ticket_source",
    "room_number",
    "ticket_service_id",
    "ticket_service_name",
  ];
  for (const key of keys) {
    const value = metadata[key];
    if (value !== undefined && value !== null && String(value).trim() !== "") {
      fallback[key] = value;
    }
  }
  if (apiResponse && typeof apiResponse === "object") {
    fallback.ticket_api_response = apiResponse;
  }
  return Object.keys(fallback).length > 0 ? fallback : null;
}

function parseProfile(input: unknown): ProfileMap | null {
  if (!input || typeof input !== "object") return null;
  const row = input as Record<string, unknown>;
  const keys = [
    "guest_id",
    "entity_id",
    "organisation_id",
    "room_number",
    "guest_phone",
    "guest_name",
    "group_id",
    "ticket_source",
    "flow",
  ];
  const parsed: ProfileMap = {};
  for (const key of keys) {
    const value = String(row[key] || "").trim();
    if (value) {
      (parsed as Record<string, string>)[key] = value;
    }
  }
  if (!parsed.organisation_id && parsed.entity_id) {
    parsed.organisation_id = parsed.entity_id;
  }
  if (!parsed.entity_id && parsed.organisation_id) {
    parsed.entity_id = parsed.organisation_id;
  }
  return Object.keys(parsed).length > 0 ? parsed : null;
}

const initialBookingDraft = {
  guest_phone: "",
  guest_name: "",
  property_name: "",
  room_number: "",
  room_type: "",
  check_in_date: "",
  check_out_date: "",
  num_guests: "1",
  status: "reserved",
};

const ChatHarness = () => {
  const runtimeConfig = useMemo(() => readEmbedRuntimeConfig(), []);
  const embedMode = runtimeConfig.embedMode;
  const widgetBrandColor = runtimeConfig.brandColor;
  const widgetAccentColor = runtimeConfig.accentColor;
  const widgetBackgroundColor = runtimeConfig.backgroundColor;
  const widgetTextColor = runtimeConfig.textColor;
  const widgetUiColor = widgetBrandColor || widgetAccentColor || WIDGET_BRAND_COLOR_DEFAULT;
  const widgetBotName = runtimeConfig.botName;
  const widgetDefaultWidth = runtimeConfig.width;
  const widgetDefaultHeight = runtimeConfig.height;
  const widgetPosition = runtimeConfig.position;
  const widgetDockClass = widgetPosition === "left" ? "left-4" : "right-4";

  const [sessionId, setSessionId] = useState(runtimeConfig.sessionId);
  const [hotelCode, setHotelCode] = useState(runtimeConfig.hotelCode);
  const [properties, setProperties] = useState<PropertyOption[]>([]);
  const [phases, setPhases] = useState<PhaseOption[]>(PHASE_FALLBACK);
  const [phase, setPhase] = useState(runtimeConfig.phase);
  const [testProfilesByPhase, setTestProfilesByPhase] = useState<Record<string, ProfileMap>>({});
  const [autoPhaseProfile, setAutoPhaseProfile] = useState(true);
  const [profileOverrides, setProfileOverrides] = useState<ProfileMap>({});
  const [extraMetadataRaw, setExtraMetadataRaw] = useState("");

  const [bookings, setBookings] = useState<BookingRow[]>([]);
  const [selectedBookingId, setSelectedBookingId] = useState<number | "">("");
  const [bookingModalOpen, setBookingModalOpen] = useState(false);
  const [bookingDraft, setBookingDraft] = useState(initialBookingDraft);
  const [bookingDraftError, setBookingDraftError] = useState("");
  const [bookingDraftSubmitting, setBookingDraftSubmitting] = useState(false);

  const [messages, setMessages] = useState<ChatMessageRow[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [prefetchBatchId, setPrefetchBatchId] = useState("");
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [inlineForm, setInlineForm] = useState<InlineFormState | null>(null);
  const [welcomeMessage, setWelcomeMessage] = useState("Hi! How can I help you today?");
  const [welcomeContext, setWelcomeContext] = useState({
    bot_name: "Assistant",
    business_name: "your property",
    city: "",
  });

  const [sessionState, setSessionState] = useState("idle");
  const [messageCount, setMessageCount] = useState(0);
  const [ticketStatus, setTicketStatus] = useState<TicketStatus>({
    label: "No ticket action",
    badge: "idle",
  });
  const [ticketDetails, setTicketDetails] = useState<Record<string, unknown> | null>(null);
  const [debugData, setDebugData] = useState<Record<string, unknown> | null>(null);
  const [widgetOpen, setWidgetOpen] = useState(embedMode);
  const [widgetMinimized, setWidgetMinimized] = useState(false);
  const [widgetExpanded, setWidgetExpanded] = useState(false);
  const [widgetShowTeaser, setWidgetShowTeaser] = useState(!embedMode);
  const isMobile = useIsMobile();

  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyData, setHistoryData] = useState<SessionHistory | null>(null);
  const [historyError, setHistoryError] = useState("");

  const [ticketsOpen, setTicketsOpen] = useState(false);
  const [ticketsLoading, setTicketsLoading] = useState(false);
  const [tickets, setTickets] = useState<TicketRow[]>([]);
  const [ticketsError, setTicketsError] = useState("");

  const suggestionRequestIdRef = useRef(0);
  const messageEndRef = useRef<HTMLDivElement | null>(null);
  const messagesScrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const postEmbedEvent = useCallback(
    (type: string, payload?: Record<string, unknown>) => {
      if (!embedMode || typeof window === "undefined" || window.parent === window) return;
      try {
        window.parent.postMessage(
          {
            source: "kebo-widget",
            type,
            widget_id: runtimeConfig.widgetId,
            hotel_code: hotelCode,
            phase: normalizePhaseId(phase) || "pre_booking",
            ...(payload || {}),
          },
          "*",
        );
      } catch {
        // no-op
      }
    },
    [embedMode, hotelCode, phase, runtimeConfig.widgetId],
  );

  const selectedBooking = useMemo(() => {
    if (selectedBookingId === "") return null;
    return bookings.find((row) => row.booking_id === selectedBookingId) || null;
  }, [bookings, selectedBookingId]);

  const resolvedWelcomeMessage = useMemo(
    () => formatWelcomeMessage(welcomeMessage, welcomeContext),
    [welcomeContext, welcomeMessage],
  );

  const activePhaseProfile = useMemo(() => {
    if (!autoPhaseProfile) return null;
    const normalized = normalizePhaseId(phase);
    if (!normalized) return null;
    const mapped = testProfilesByPhase[normalized];
    if (!mapped) return null;
    const resolved: ProfileMap = { ...mapped };
    for (const [key, value] of Object.entries(profileOverrides)) {
      if (!String(value || "").trim()) continue;
      (resolved as Record<string, string>)[key] = String(value).trim();
    }
    if (!resolved.organisation_id && resolved.entity_id) {
      resolved.organisation_id = resolved.entity_id;
    }
    if (!resolved.entity_id && resolved.organisation_id) {
      resolved.entity_id = resolved.organisation_id;
    }
    return resolved;
  }, [autoPhaseProfile, phase, profileOverrides, testProfilesByPhase]);

  const updateSessionUiFromResponse = useCallback((payload: ChatApiResponse) => {
    const nextState = String(payload.state || "idle").trim() || "idle";
    setSessionState(nextState);
    const metadata = (payload.metadata || {}) as Record<string, unknown>;
    const nextMessageCount = Number(metadata.message_count || 0);
    if (Number.isFinite(nextMessageCount) && nextMessageCount > 0) {
      setMessageCount(nextMessageCount);
    } else {
      setMessageCount((prev) => prev + 1);
    }
    setTicketStatus(resolveTicketStatus(metadata));
    setTicketDetails(resolveTicketDetails(metadata));
  }, []);

  const resetLocalConversation = useCallback((newSessionId: string) => {
    setSessionId(newSessionId);
    setMessages([
      {
        id: `welcome_${Date.now()}`,
        role: "assistant",
        content: resolvedWelcomeMessage,
        canonicalContent: resolvedWelcomeMessage,
      },
    ]);
    setSuggestions([]);
    setPrefetchBatchId("");
    setInlineForm(null);
    setSessionState("idle");
    setMessageCount(0);
    setTicketStatus({ label: "No ticket action", badge: "idle" });
    setTicketDetails(null);
    setDebugData(null);
    suggestionRequestIdRef.current += 1;
  }, [resolvedWelcomeMessage]);

  const fetchAndShowSuggestions = useCallback(
    async (
      lastBotMessage: string,
      userMessage: string,
      fallbackSuggestions: string[],
      assistantTurnId: string,
    ) => {
      if (inlineForm && !inlineForm.successMessage) {
        setSuggestions([]);
        setPrefetchBatchId("");
        return;
      }
      const requestId = ++suggestionRequestIdRef.current;
      setSuggestions([]);
      setPrefetchBatchId("");
      try {
        const response = await fetch("/api/chat/suggestions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            last_bot_message: lastBotMessage,
            user_message: userMessage,
            hotel_code: hotelCode,
            current_phase: phase,
            session_id: sessionId,
            assistant_turn_id: assistantTurnId,
            fallback_suggestions: Array.isArray(fallbackSuggestions) ? fallbackSuggestions : [],
          }),
        });
        if (!response.ok) {
          if (
            requestId === suggestionRequestIdRef.current &&
            fallbackSuggestions.length > 0 &&
            (!inlineForm || inlineForm.successMessage)
          ) {
            setSuggestions(fallbackSuggestions);
            setPrefetchBatchId("");
          }
          return;
        }
        const data = (await response.json()) as SuggestionsApiResponse;
        if (requestId !== suggestionRequestIdRef.current) return;
        if (inlineForm && !inlineForm.successMessage) return;
        const next = Array.isArray(data.suggestions) ? data.suggestions.filter(Boolean) : [];
        if (next.length > 0) {
          setSuggestions(next);
          setPrefetchBatchId(String(data.prefetch_batch_id || "").trim());
          return;
        }
        if (fallbackSuggestions.length > 0) {
          setSuggestions(fallbackSuggestions);
          setPrefetchBatchId("");
        }
      } catch {
        if (
          requestId === suggestionRequestIdRef.current &&
          fallbackSuggestions.length > 0 &&
          (!inlineForm || inlineForm.successMessage)
        ) {
          setSuggestions(fallbackSuggestions);
          setPrefetchBatchId("");
        }
      }
    },
    [hotelCode, inlineForm, phase, sessionId],
  );

  const loadProperties = useCallback(async () => {
    if (embedMode) {
      const embeddedCode = String(runtimeConfig.hotelCode || "default").trim().toLowerCase() || "default";
      setProperties([{ code: embeddedCode, name: embeddedCode, city: "" }]);
      setHotelCode(embeddedCode);
      return;
    }
    try {
      const response = await fetch("/api/chat/properties");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as { properties?: Array<Record<string, unknown>> };
      const rows = Array.isArray(payload.properties) ? payload.properties : [];
      const parsed = rows
        .map((row) => {
          const code = String(row.code || "").trim().toLowerCase();
          if (!code) return null;
          return {
            code,
            name: String(row.name || code).trim() || code,
            city: String(row.city || "").trim(),
          } satisfies PropertyOption;
        })
        .filter((row): row is PropertyOption => Boolean(row));
      if (parsed.length === 0) throw new Error("No properties");
      setProperties(parsed);
      setHotelCode((prev) => {
        const current = String(prev || "").trim().toLowerCase();
        if (parsed.some((row) => row.code === current)) return current;
        return parsed[0].code;
      });
    } catch {
      setProperties([{ code: "default", name: "Default Property", city: "" }]);
      setHotelCode("default");
    }
  }, [embedMode, runtimeConfig.hotelCode]);

  const loadTestProfiles = useCallback(async () => {
    if (embedMode) {
      setTestProfilesByPhase({});
      setAutoPhaseProfile(false);
      setPhases(PHASE_FALLBACK);
      setPhase(normalizePhaseId(runtimeConfig.phase) || "pre_booking");
      return;
    }
    try {
      const response = await fetch(`/api/chat/test-profiles?hotel_code=${encodeURIComponent(hotelCode || "default")}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as {
        auto_apply_enabled?: boolean;
        profiles_by_phase?: Record<string, unknown>;
        phases?: Array<Record<string, unknown>>;
      };
      const mapped: Record<string, ProfileMap> = {};
      if (payload.profiles_by_phase && typeof payload.profiles_by_phase === "object") {
        for (const [key, value] of Object.entries(payload.profiles_by_phase)) {
          const normalized = normalizePhaseId(key);
          if (!normalized) continue;
          const parsed = parseProfile(value);
          if (parsed) {
            mapped[normalized] = parsed;
          }
        }
      }
      setTestProfilesByPhase(mapped);
      setAutoPhaseProfile(payload.auto_apply_enabled !== false);

      const phaseRows = Array.isArray(payload.phases) ? payload.phases : [];
      const parsedPhases = phaseRows
        .map((row) => {
          const id = normalizePhaseId(row.id);
          if (!id) return null;
          return {
            id,
            name: String(row.name || id.replace(/_/g, " ")).trim(),
          } satisfies PhaseOption;
        })
        .filter((row): row is PhaseOption => Boolean(row));
      const nextPhases = parsedPhases.length > 0 ? parsedPhases : PHASE_FALLBACK;
      setPhases(nextPhases);
      setPhase((prev) => {
        const normalized = normalizePhaseId(prev);
        if (normalized && nextPhases.some((row) => row.id === normalized)) return normalized;
        return nextPhases[0]?.id || "pre_booking";
      });
    } catch {
      setTestProfilesByPhase({});
      setAutoPhaseProfile(false);
      setPhases(PHASE_FALLBACK);
      setPhase((prev) => normalizePhaseId(prev) || "pre_booking");
    }
  }, [embedMode, hotelCode, runtimeConfig.phase]);

  const loadBusinessWelcome = useCallback(async () => {
    try {
      const response = await fetch("/admin/api/config/onboarding/business", {
        headers: { "x-hotel-code": String(hotelCode || "default").trim().toLowerCase() || "default" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as Record<string, unknown>;
      setWelcomeContext({
        bot_name: String(payload.bot_name || "Assistant").trim() || "Assistant",
        business_name: String(payload.name || payload.business_name || "your property").trim() || "your property",
        city: String(payload.city || "").trim(),
      });
      setWelcomeMessage(String(payload.welcome_message || "").trim() || "Hi! How can I help you today?");
    } catch {
      setWelcomeContext({
        bot_name: "Assistant",
        business_name: "your property",
        city: "",
      });
      setWelcomeMessage("Hi! How can I help you today?");
    }
  }, [hotelCode]);

  const loadBookings = useCallback(async () => {
    if (embedMode) {
      setBookings([]);
      setSelectedBookingId("");
      return;
    }
    const normalizedPhase = normalizePhaseId(phase);
    if (!normalizedPhase || normalizedPhase === "pre_booking") {
      setBookings([]);
      setSelectedBookingId("");
      return;
    }
    try {
      const response = await fetch(
        `/admin/api/bookings?hotel_code=${encodeURIComponent(hotelCode || "default")}&phase=${encodeURIComponent(normalizedPhase)}`,
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as { bookings?: BookingRow[] };
      const rows = Array.isArray(payload.bookings) ? payload.bookings : [];
      setBookings(rows);
      setSelectedBookingId((prev) => {
        if (typeof prev === "number" && rows.some((row) => row.booking_id === prev)) return prev;
        if (rows.length === 1) return rows[0].booking_id;
        return "";
      });
    } catch {
      setBookings([]);
      setSelectedBookingId("");
    }
  }, [embedMode, hotelCode, phase]);

  useEffect(() => {
    void loadProperties();
  }, [loadProperties]);

  useEffect(() => {
    if (!hotelCode) return;
    void loadTestProfiles();
    void loadBusinessWelcome();
    void loadBookings();
  }, [hotelCode, loadBookings, loadBusinessWelcome, loadTestProfiles]);

  useEffect(() => {
    void loadBookings();
  }, [loadBookings, phase]);

  useEffect(() => {
    if (!embedMode) return;
    postEmbedEvent("widget:ready");
  }, [embedMode, hotelCode, phase, postEmbedEvent]);

  useEffect(() => {
    const panel = messagesScrollRef.current;
    if (!panel) return;
    panel.scrollTop = panel.scrollHeight;
  }, [messages, suggestions, inlineForm, isSending]);

  useEffect(() => {
    if (!embedMode || typeof document === "undefined") return;
    const html = document.documentElement;
    const body = document.body;
    const root = document.getElementById("chat-root");

    const prevHtmlHeight = html.style.height;
    const prevHtmlOverflow = html.style.overflow;
    const prevBodyHeight = body.style.height;
    const prevBodyOverflow = body.style.overflow;
    const prevRootHeight = root?.style.height || "";
    const prevRootOverflow = root?.style.overflow || "";

    html.style.height = "100%";
    html.style.overflow = "hidden";
    body.style.height = "100%";
    body.style.overflow = "hidden";
    if (root) {
      root.style.height = "100%";
      root.style.overflow = "hidden";
    }

    return () => {
      html.style.height = prevHtmlHeight;
      html.style.overflow = prevHtmlOverflow;
      body.style.height = prevBodyHeight;
      body.style.overflow = prevBodyOverflow;
      if (root) {
        root.style.height = prevRootHeight;
        root.style.overflow = prevRootOverflow;
      }
    };
  }, [embedMode]);

  useEffect(() => {
    if (messages.length === 0) {
      setMessages([
        {
          id: `welcome_${Date.now()}`,
          role: "assistant",
          content: resolvedWelcomeMessage,
          canonicalContent: resolvedWelcomeMessage,
        },
      ]);
      return;
    }
    if (
      messages.length === 1 &&
      messages[0]?.id?.startsWith("welcome_") &&
      String(messages[0].content || "") !== resolvedWelcomeMessage
    ) {
      setMessages([
        {
          id: messages[0].id,
          role: "assistant",
          content: resolvedWelcomeMessage,
          canonicalContent: resolvedWelcomeMessage,
        },
      ]);
    }
  }, [messages, resolvedWelcomeMessage]);

  const buildRequestMetadata = useCallback(
    (interactionMeta?: { source_type?: string; source_label?: string; source_text?: string }) => {
      const phaseId = normalizePhaseId(phase) || "pre_booking";
      const metadata: Record<string, unknown> = {
        phase: phaseId,
        chat_test_profile_applied: false,
        chat_test_profile_phase: phaseId,
      };

      if (interactionMeta) {
        const sourceType = String(interactionMeta.source_type || "").trim();
        const sourceLabel = String(interactionMeta.source_label || "").trim();
        const sourceText = String(interactionMeta.source_text || "").trim();
        if (sourceType) metadata.ui_source_type = sourceType;
        if (sourceLabel) metadata.ui_source_label = sourceLabel;
        if (sourceText) metadata.ui_source_text = sourceText;
        metadata.ui_event_at = new Date().toISOString();
      }

      if (activePhaseProfile) {
        for (const [key, value] of Object.entries(activePhaseProfile)) {
          if (!String(value || "").trim()) continue;
          metadata[key] = value;
        }
        metadata.chat_test_profile_applied = true;
      }

      if (selectedBooking) {
        metadata.booking_id = selectedBooking.booking_id;
        metadata.booking_guest_id = selectedBooking.guest_id || "";
        metadata.booking_confirmation_code = selectedBooking.confirmation_code || "";
        metadata.booking_property_name = selectedBooking.property_name || "";
        metadata.booking_room_number = selectedBooking.room_number || "";
        metadata.booking_room_type = selectedBooking.room_type || "";
        metadata.booking_check_in_date = selectedBooking.check_in_date || "";
        metadata.booking_check_out_date = selectedBooking.check_out_date || "";
        metadata.booking_guest_name = selectedBooking.guest_name || "";
        metadata.booking_guest_phone = selectedBooking.guest_phone || "";
        metadata.booking_status = selectedBooking.status || "";
        metadata.booking_phase = selectedBooking.phase || "";
      }

      if (extraMetadataRaw.trim()) {
        try {
          const parsed = JSON.parse(extraMetadataRaw);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            Object.assign(metadata, parsed as Record<string, unknown>);
          }
        } catch {
          metadata.extra_metadata_parse_error = "Invalid JSON";
        }
      }

      if (!metadata.organisation_id && metadata.entity_id) {
        metadata.organisation_id = metadata.entity_id;
      }
      if (!metadata.entity_id && metadata.organisation_id) {
        metadata.entity_id = metadata.organisation_id;
      }

      return metadata;
    },
    [activePhaseProfile, extraMetadataRaw, phase, selectedBooking],
  );

  const sendMessage = useCallback(
    async (rawMessage: string, sourceType = "typed_input") => {
      const text = String(rawMessage || "").trim();
      if (!text || isSending) return;

      const userId = `u_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
      setWidgetOpen(true);
      setInput("");
      setSuggestions([]);
      setPrefetchBatchId("");
      setMessages((prev) => [
        ...prev,
        { id: userId, role: "user", content: text, canonicalContent: text },
      ]);
      setIsSending(true);

      try {
        const requestMetadata = buildRequestMetadata({
          source_type: sourceType,
          source_label: sourceType === "typed_input" ? "typed_input" : text,
          source_text: text,
        });
        if (prefetchBatchId) {
          requestMetadata.prefetch_batch_id = prefetchBatchId;
          requestMetadata.ui_prefetch_batch_id = prefetchBatchId;
        }
        const response = await fetch("/api/chat/message", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId,
            message: text,
            hotel_code: hotelCode,
            channel: "web_widget",
            metadata: requestMetadata,
          }),
        });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const data = (await response.json()) as ChatApiResponse;
        const assistantMessage = resolveAssistantDisplayMessage(data) || "No response";
        const assistantId = `a_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
        setMessages((prev) => [
          ...prev,
          {
            id: assistantId,
            role: "assistant",
            content: assistantMessage,
            canonicalContent: String(data.message || assistantMessage),
            raw: data,
          },
        ]);

        setDebugData(data as Record<string, unknown>);
        updateSessionUiFromResponse(data);

        const metadata = (data.metadata || {}) as Record<string, unknown>;
        const assistantTurnId = String(metadata.turn_trace_id || "").trim();
        const fields = normalizeFormFields(
          Array.isArray(metadata.form_fields) ? metadata.form_fields : data.form_fields,
        );
        const rawFormTrigger = metadata.form_trigger;
        const explicitFormTrigger =
          rawFormTrigger === true || rawFormTrigger === "true" || rawFormTrigger === 1 || rawFormTrigger === "1";
        const stateAwaitingInfo = String(data.state || "")
          .trim()
          .toLowerCase() === "awaiting_info";
        const serviceLabel = String(data.service_llm_label || metadata.service_llm_label || "")
          .trim()
          .toLowerCase();
        const displayText = String(data.display_message || metadata.display_message || data.message || "")
          .trim()
          .toLowerCase();
        const messageHintsCollection =
          displayText.includes("please fill in the details below") ||
          displayText.includes("please fill in the booking details below") ||
          displayText.includes("please fill in the form below");
        const inferredFormTrigger =
          stateAwaitingInfo && Boolean(serviceLabel) && serviceLabel !== "main" && fields.length > 0 && messageHintsCollection;
        const shouldShowInlineForm = (explicitFormTrigger || inferredFormTrigger) && fields.length > 0;
        const orchestrationDecision = (metadata.orchestration_decision || metadata.decision || {}) as Record<string, unknown>;
        const resolvedServiceId = String(
          metadata.form_service_id ||
            data.form_service_id ||
            metadata.pending_service_id ||
            metadata.orchestration_target_service_id ||
            orchestrationDecision.target_service_id ||
            serviceLabel ||
            "",
        ).trim();

        if (shouldShowInlineForm) {
          const values: Record<string, string> = {};
          const countryCodes: Record<string, string> = {};
          for (const field of fields) {
            values[field.id] = "";
            if (isPhoneField(field)) {
              countryCodes[field.id] = "+91";
            }
          }
          setInlineForm({
            messageId: assistantId,
            serviceId: resolvedServiceId,
            fields,
            values,
            countryCodes,
            errors: {},
            submitting: false,
            successMessage: "",
          });
          setSuggestions([]);
          setPrefetchBatchId("");
        } else {
          setInlineForm(null);
          const runtimeSuggestions = Array.isArray(data.suggested_actions)
            ? data.suggested_actions.filter(Boolean)
            : [];
          const stateValue = String(data.state || "").toLowerCase();
          const useRuntimeDirectly =
            runtimeSuggestions.length > 0 &&
            (stateValue === "awaiting_confirmation" || stateValue === "escalated");
          if (useRuntimeDirectly) {
            setSuggestions(runtimeSuggestions);
            setPrefetchBatchId("");
          } else {
            void fetchAndShowSuggestions(
              String(data.message || ""),
              text,
              runtimeSuggestions,
              assistantTurnId,
            );
          }
        }
      } catch (error) {
        const message =
          error instanceof Error
            ? `Sorry, failed to process this message (${error.message}).`
            : "Sorry, failed to process this message.";
        setMessages((prev) => [
          ...prev,
          {
            id: `a_error_${Date.now()}`,
            role: "assistant",
            content: message,
            canonicalContent: message,
          },
        ]);
        setSuggestions([]);
        setPrefetchBatchId("");
      } finally {
        setIsSending(false);
        setTimeout(() => {
          inputRef.current?.focus();
        }, 0);
      }
    },
    [
      buildRequestMetadata,
      fetchAndShowSuggestions,
      hotelCode,
      isSending,
      prefetchBatchId,
      sessionId,
      updateSessionUiFromResponse,
    ],
  );

  const handleInlineFormSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (!inlineForm || !inlineForm.serviceId || inlineForm.submitting || inlineForm.successMessage) return;

      const composedValues: Record<string, string> = {};
      for (const field of inlineForm.fields) {
        const rawValue = String(inlineForm.values[field.id] || "").trim();
        if (isPhoneField(field) && rawValue) {
          const code = String(inlineForm.countryCodes[field.id] || "+91").trim() || "+91";
          composedValues[field.id] = rawValue.startsWith("+") ? rawValue : `${code}${rawValue}`;
          continue;
        }
        composedValues[field.id] = rawValue;
      }

      setInlineForm((prev) =>
        prev
          ? {
              ...prev,
              submitting: true,
              errors: {},
            }
          : prev,
      );

      try {
        const response = await fetch("/api/chat/form-submit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId,
            hotel_code: hotelCode,
            service_id: inlineForm.serviceId,
            form_data: composedValues,
            metadata: buildRequestMetadata(),
          }),
        });
        const result = (await response.json()) as {
          success?: boolean;
          message?: string;
          ticket_id?: string;
          errors?: Array<{ id?: string; field_id?: string; message?: string }>;
        };

        if (result.success) {
          setInlineForm((prev) =>
            prev
              ? {
                  ...prev,
                  submitting: false,
                  successMessage: String(result.message || "Submitted successfully."),
                  errors: {},
                }
              : prev,
          );
          setSessionState("completed");
          setTicketStatus({
            label: `Created: ${String(result.ticket_id || "unknown-id")}`,
            badge: "created",
          });
          setMessageCount((prev) => prev + 1);
          setSuggestions([]);
          setPrefetchBatchId("");
          return;
        }

        const errors: Record<string, string> = {};
        if (Array.isArray(result.errors) && result.errors.length > 0) {
          for (const row of result.errors) {
            const key = String(row.field_id || row.id || "").trim();
            if (!key) continue;
            errors[key] = String(row.message || "Please correct this field.");
          }
        } else {
          errors._global = String(result.message || "Submission failed. Please try again.");
        }
        setInlineForm((prev) =>
          prev
            ? {
                ...prev,
                submitting: false,
                errors,
              }
            : prev,
        );
      } catch {
        setInlineForm((prev) =>
          prev
            ? {
                ...prev,
                submitting: false,
                errors: { _global: "Network error. Please try again." },
              }
            : prev,
        );
      }
    },
    [buildRequestMetadata, hotelCode, inlineForm, sessionId],
  );

  const handleResetSession = useCallback(async () => {
    try {
      const response = await fetch(`/api/chat/session/${encodeURIComponent(sessionId)}/reset`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      setSessionState("idle");
      setTicketStatus({ label: "No ticket action", badge: "idle" });
      setTicketDetails(null);
      setInlineForm(null);
      setSuggestions([]);
      setPrefetchBatchId("");
      setMessages([
        {
          id: `welcome_${Date.now()}`,
          role: "assistant",
          content: resolvedWelcomeMessage,
          canonicalContent: resolvedWelcomeMessage,
        },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          id: `a_reset_error_${Date.now()}`,
          role: "assistant",
          content: "Failed to reset the session.",
          canonicalContent: "Failed to reset the session.",
        },
      ]);
    }
  }, [resolvedWelcomeMessage, sessionId]);

  const handleLoadHistory = useCallback(async () => {
    setHistoryOpen(true);
    setHistoryLoading(true);
    setHistoryError("");
    try {
      const response = await fetch(`/api/chat/session/${encodeURIComponent(sessionId)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as SessionHistory;
      setHistoryData(payload);
    } catch (error) {
      setHistoryData(null);
      setHistoryError(error instanceof Error ? error.message : "Failed to load history");
    } finally {
      setHistoryLoading(false);
    }
  }, [sessionId]);

  const handleLoadTickets = useCallback(async () => {
    setTicketsOpen(true);
    setTicketsLoading(true);
    setTicketsError("");
    try {
      const response = await fetch("/admin/api/tickets");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as { tickets?: TicketRow[] };
      setTickets(Array.isArray(payload.tickets) ? payload.tickets : []);
    } catch (error) {
      setTickets([]);
      setTicketsError(error instanceof Error ? error.message : "Failed to load tickets");
    } finally {
      setTicketsLoading(false);
    }
  }, []);

  const handleCreateBooking = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setBookingDraftSubmitting(true);
      setBookingDraftError("");

      const body = {
        ...bookingDraft,
        num_guests: Number(bookingDraft.num_guests || "1") || 1,
      };

      try {
        const response = await fetch(`/admin/api/bookings?hotel_code=${encodeURIComponent(hotelCode || "default")}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(String((payload as Record<string, unknown>).detail || response.statusText || "Failed"));
        }
        setBookingModalOpen(false);
        setBookingDraft(initialBookingDraft);
        await loadBookings();
      } catch (error) {
        setBookingDraftError(error instanceof Error ? error.message : "Failed to create booking");
      } finally {
        setBookingDraftSubmitting(false);
      }
    },
    [bookingDraft, hotelCode, loadBookings],
  );

  const profileStatusText = useMemo(() => {
    const phaseId = normalizePhaseId(phase) || "unknown";
    if (!autoPhaseProfile) return `Auto profile mapping is OFF for ${phaseId}.`;
    if (!activePhaseProfile) return `No mapped test profile for ${phaseId}.`;
    return `Mapped test profile active for ${phaseId}.`;
  }, [activePhaseProfile, autoPhaseProfile, phase]);

  const bookingDetailsText = useMemo(() => {
    if (!selectedBooking) return "No booking selected.";
    return [
      `Code: ${selectedBooking.confirmation_code || "-"}`,
      `Guest: ${selectedBooking.guest_name || "-"} (${selectedBooking.guest_phone || "-"})`,
      `Property: ${selectedBooking.property_name || "-"}`,
      `Room: ${selectedBooking.room_number || "-"} (${selectedBooking.room_type || "-"})`,
      `Dates: ${selectedBooking.check_in_date || "-"} to ${selectedBooking.check_out_date || "-"}`,
      `Status: ${selectedBooking.status || "-"} | Phase: ${selectedBooking.phase || "-"}`,
    ].join("\n");
  }, [selectedBooking]);

  const renderInlineForm = () => {
    if (!inlineForm) return null;
    const bounds = getStayDateBounds(phase, selectedBooking);
    return (
      <form
        onSubmit={handleInlineFormSubmit}
        className="mt-3 rounded-xl p-3"
        style={{ backgroundColor: `${widgetUiColor}08`, border: `1px solid ${widgetUiColor}20` }}
      >
        {inlineForm.successMessage ? (
          <div
            className="rounded-lg px-3 py-2 text-sm"
            style={{ backgroundColor: `${widgetUiColor}12`, color: widgetUiColor, border: `1px solid ${widgetUiColor}25` }}
          >
            {inlineForm.successMessage}
          </div>
        ) : (
          <>
            {inlineForm.errors._global ? (
              <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
                {inlineForm.errors._global}
              </div>
            ) : null}
            <div className="grid grid-cols-1 gap-2">
              {inlineForm.fields.map((field) => {
                const fieldError = inlineForm.errors[field.id];
                const commonClass = `w-full rounded-lg border px-2.5 py-1.5 text-sm outline-none transition-colors ${
                  fieldError
                    ? "border-rose-400 bg-rose-50"
                    : "bg-white"
                }`;
                const commonStyle: React.CSSProperties = fieldError
                  ? {}
                  : { borderColor: `${widgetUiColor}30` };
                const commonFocusHandler = (e: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
                  if (!fieldError) e.currentTarget.style.borderColor = widgetUiColor;
                };
                const commonBlurHandler = (e: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
                  if (!fieldError) e.currentTarget.style.borderColor = `${widgetUiColor}30`;
                };
                const key = `inline_${field.id}`;

                if (field.type === "textarea") {
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <textarea
                        className={commonClass}
                        style={commonStyle}
                        onFocus={commonFocusHandler as any}
                        onBlur={commonBlurHandler as any}
                        rows={3}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setInlineForm((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  values: { ...prev.values, [field.id]: value },
                                  errors: { ...prev.errors, [field.id]: "" },
                                }
                              : prev,
                          );
                        }}
                      />
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if (isPhoneField(field)) {
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <div className="flex gap-2">
                        <select
                          className="w-24 rounded-lg border bg-white px-2 py-1.5 text-sm outline-none transition-colors"
                          style={{ borderColor: `${widgetUiColor}30` }}
                          onFocus={(e) => { e.currentTarget.style.borderColor = widgetUiColor; }}
                          onBlur={(e) => { e.currentTarget.style.borderColor = `${widgetUiColor}30`; }}
                          value={inlineForm.countryCodes[field.id] || "+91"}
                          onChange={(event) => {
                            const code = event.target.value;
                            setInlineForm((prev) =>
                              prev
                                ? {
                                    ...prev,
                                    countryCodes: { ...prev.countryCodes, [field.id]: code },
                                  }
                                : prev,
                            );
                          }}
                        >
                          {PHONE_CODES.map((code) => (
                            <option key={`${key}_${code}`} value={code}>
                              {code}
                            </option>
                          ))}
                        </select>
                        <input
                          type="tel"
                          className={commonClass}
                          style={commonStyle}
                          onFocus={commonFocusHandler}
                          onBlur={commonBlurHandler}
                          value={inlineForm.values[field.id] || ""}
                          onChange={(event) => {
                            const value = event.target.value;
                            setInlineForm((prev) =>
                              prev
                                ? {
                                    ...prev,
                                    values: { ...prev.values, [field.id]: value },
                                    errors: { ...prev.errors, [field.id]: "" },
                                  }
                                : prev,
                            );
                          }}
                        />
                      </div>
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if (field.type === "date") {
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <input
                        type="date"
                        className={commonClass}
                        style={commonStyle}
                        onFocus={commonFocusHandler}
                        onBlur={commonBlurHandler}
                        min={bounds.min || undefined}
                        max={bounds.max || undefined}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setInlineForm((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  values: { ...prev.values, [field.id]: value },
                                  errors: { ...prev.errors, [field.id]: "" },
                                }
                              : prev,
                          );
                        }}
                      />
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if (field.type === "time") {
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <input
                        type="time"
                        className={commonClass}
                        style={commonStyle}
                        onFocus={commonFocusHandler}
                        onBlur={commonBlurHandler}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setInlineForm((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  values: { ...prev.values, [field.id]: value },
                                  errors: { ...prev.errors, [field.id]: "" },
                                }
                              : prev,
                          );
                        }}
                      />
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if (field.type === "datetime-local" || field.type === "datetime") {
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <input
                        type="datetime-local"
                        className={commonClass}
                        style={commonStyle}
                        onFocus={commonFocusHandler}
                        onBlur={commonBlurHandler}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setInlineForm((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  values: { ...prev.values, [field.id]: value },
                                  errors: { ...prev.errors, [field.id]: "" },
                                }
                              : prev,
                          );
                        }}
                      />
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if ((field.type === "select" || field.type === "dropdown") && field.options.length > 0) {
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <select
                        className={commonClass}
                        style={commonStyle}
                        onFocus={commonFocusHandler as any}
                        onBlur={commonBlurHandler as any}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setInlineForm((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  values: { ...prev.values, [field.id]: value },
                                  errors: { ...prev.errors, [field.id]: "" },
                                }
                              : prev,
                          );
                        }}
                      >
                        <option value="">Select...</option>
                        {field.options.map((option) => (
                          <option key={`${key}_${option}`} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                return (
                  <label key={key} className="flex flex-col gap-1">
                    <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                      {field.label}
                      {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                    </span>
                    <input
                      type={field.type === "number" ? "number" : field.type === "email" ? "email" : "text"}
                      className={commonClass}
                      style={commonStyle}
                      onFocus={commonFocusHandler}
                      onBlur={commonBlurHandler}
                      placeholder={field.placeholder || undefined}
                      value={inlineForm.values[field.id] || ""}
                      onChange={(event) => {
                        const value = event.target.value;
                        setInlineForm((prev) =>
                          prev
                            ? {
                                ...prev,
                                values: { ...prev.values, [field.id]: value },
                                errors: { ...prev.errors, [field.id]: "" },
                              }
                            : prev,
                        );
                      }}
                    />
                    {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                  </label>
                );
              })}
            </div>
            <div className="mt-3">
              <button
                type="submit"
                disabled={inlineForm.submitting}
                className="w-full rounded-lg px-3 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-50"
                style={{ backgroundColor: widgetUiColor }}
              >
                {inlineForm.submitting ? "Submitting..." : "Submit"}
              </button>
            </div>
          </>
        )}
      </form>
    );
  };

  return (
    <div
      className={
        embedMode
          ? "h-screen w-full overflow-hidden font-sans"
          : "min-h-screen bg-[radial-gradient(circle_at_top,_#f8fafc_0%,_#e2e8f0_55%,_#cbd5e1_100%)] text-slate-900 font-sans"
      }
      style={embedMode ? { backgroundColor: widgetBackgroundColor, color: widgetTextColor } : undefined}
    >
      {!embedMode && (
      <div className="mx-auto max-w-6xl p-4 sm:p-6">
        <div className="rounded-xl border border-slate-300/70 bg-white/85 p-4 shadow-sm backdrop-blur">
          <h1 className="text-lg font-semibold">Chat Widget Test Harness</h1>
          <p className="mt-1 text-sm text-slate-600">
            Configure hotel, phase, profile, booking, and metadata; then test through the same widget flow used in production.
          </p>

          <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-600">Hotel</span>
              <select
                className="rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
                value={hotelCode}
                onChange={(event) => setHotelCode(event.target.value)}
              >
                {properties.map((row) => (
                  <option key={row.code} value={row.code}>
                    {[row.name, row.city].filter(Boolean).join(" - ") || row.code}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-600">Phase</span>
              <select
                className="rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
                value={phase}
                onChange={(event) => setPhase(normalizePhaseId(event.target.value) || "pre_booking")}
              >
                {phases.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.name}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-600">Booking (phase scoped)</span>
              <select
                className="rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
                value={selectedBookingId === "" ? "" : String(selectedBookingId)}
                onChange={(event) => {
                  const value = event.target.value;
                  setSelectedBookingId(value ? Number(value) : "");
                }}
              >
                <option value="">{normalizePhaseId(phase) === "pre_booking" ? "N/A (pre-booking)" : "-- Select --"}</option>
                {bookings.map((row) => (
                  <option key={row.booking_id} value={row.booking_id}>
                    {`${row.guest_name || "Guest"} - ${row.room_number || "No room"} - ${row.property_name || ""}`}
                  </option>
                ))}
              </select>
            </label>

            <div className="flex items-end gap-2">
              <button
                type="button"
                className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                onClick={() => setBookingModalOpen(true)}
              >
                Create Booking
              </button>
              <button
                type="button"
                className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                onClick={() => {
                  resetLocalConversation(generateSessionId());
                }}
              >
                New Session
              </button>
            </div>
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <div className="rounded border border-slate-200 p-3">
              <div className="mb-2 flex items-center gap-2">
                <input
                  id="auto-phase-profile"
                  type="checkbox"
                  checked={autoPhaseProfile}
                  onChange={(event) => setAutoPhaseProfile(event.target.checked)}
                />
                <label htmlFor="auto-phase-profile" className="text-sm font-medium">
                  Auto-apply mapped test profile
                </label>
              </div>
              <p className="text-xs text-slate-600">{profileStatusText}</p>
              <div className="mt-2 grid gap-2">
                {(["guest_name", "guest_phone", "room_number", "guest_id", "entity_id", "organisation_id", "ticket_source", "flow"] as const).map(
                  (field) => (
                    <label key={field} className="flex flex-col gap-1">
                      <span className="text-[11px] uppercase tracking-wide text-slate-500">{field}</span>
                      <input
                        className="rounded border border-slate-300 bg-white px-2 py-1 text-sm"
                        value={profileOverrides[field] || ""}
                        placeholder={activePhaseProfile?.[field] || ""}
                        onChange={(event) =>
                          setProfileOverrides((prev) => ({
                            ...prev,
                            [field]: event.target.value,
                          }))
                        }
                      />
                    </label>
                  ),
                )}
              </div>
              <pre className="mt-2 max-h-28 overflow-auto rounded bg-slate-100 p-2 text-xs text-slate-700">
                {JSON.stringify(activePhaseProfile || {}, null, 2)}
              </pre>
            </div>
            <div className="rounded border border-slate-200 p-3">
              <p className="text-sm font-medium">Session & Ticket Snapshot</p>
              <div className="mt-2 grid gap-1 text-sm">
                <p>
                  <span className="font-medium">Session:</span> {sessionId}
                </p>
                <p>
                  <span className="font-medium">State:</span> {sessionState}
                </p>
                <p>
                  <span className="font-medium">Ticket:</span> {ticketStatus.label}
                </p>
                <p>
                  <span className="font-medium">Messages:</span> {messageCount}
                </p>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={handleResetSession}
                  className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                >
                  Reset Session
                </button>
                <button
                  type="button"
                  onClick={handleLoadHistory}
                  className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                >
                  View History
                </button>
                <button
                  type="button"
                  onClick={handleLoadTickets}
                  className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                >
                  View Tickets
                </button>
              </div>
              <label className="mt-3 block">
                <span className="text-xs font-medium text-slate-600">Extra metadata JSON (optional)</span>
                <textarea
                  value={extraMetadataRaw}
                  onChange={(event) => setExtraMetadataRaw(event.target.value)}
                  className="mt-1 h-20 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  placeholder='{"user_type":"tester","channel_origin":"widget"}'
                />
              </label>
              <pre className="mt-2 max-h-28 overflow-auto rounded bg-slate-100 p-2 text-xs text-slate-700">{bookingDetailsText}</pre>
              {ticketDetails ? (
                <pre className="mt-2 max-h-28 overflow-auto rounded bg-emerald-50 p-2 text-xs text-emerald-900">
                  {JSON.stringify(ticketDetails, null, 2)}
                </pre>
              ) : null}
            </div>
          </div>

          {debugData ? (
            <details className="mt-3 rounded border border-slate-200 bg-slate-50 p-3">
              <summary className="cursor-pointer text-sm font-medium">Debug Payload</summary>
              <pre className="mt-2 max-h-64 overflow-auto text-xs text-slate-700">{JSON.stringify(debugData, null, 2)}</pre>
            </details>
          ) : null}
        </div>
      </div>
      )}

      <div className={embedMode ? "h-full w-full" : `fixed bottom-4 z-[9999] ${widgetDockClass}`}>
        <AnimatePresence>
          {!embedMode && !widgetOpen && (
            <motion.div
              initial={{ scale: 0, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0, opacity: 0 }}
              className={`flex flex-col gap-2 ${widgetPosition === "left" ? "items-start" : "items-end"}`}
            >
              {widgetShowTeaser && (
                <motion.div
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="relative max-w-[220px] rounded-xl border border-slate-200 bg-white p-3 shadow-lg"
                >
                  <button
                    type="button"
                    onClick={() => setWidgetShowTeaser(false)}
                    className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full border border-slate-200 bg-white text-[10px] text-slate-500 shadow-sm hover:bg-slate-100"
                  >
                    ×
                  </button>
                  <p className="text-xs font-medium text-slate-900">👋 Hi there!</p>
                  <p className="mt-0.5 text-[11px] text-slate-500">
                    Need help? Chat with {widgetBotName}
                  </p>
                </motion.div>
              )}

              <button
                type="button"
                onClick={() => {
                  setWidgetOpen(true);
                  setWidgetShowTeaser(false);
                }}
                className="flex h-14 w-14 items-center justify-center rounded-full shadow-lg transition-transform hover:scale-105 active:scale-95"
                style={{
                  backgroundColor: widgetBrandColor,
                  boxShadow: `0 8px 24px -4px rgba(0,0,0,0.2), 0 0 24px -8px ${widgetBrandColor}80`,
                  border: "3px solid rgba(255,255,255,0.9)",
                }}
                aria-label="Open Chat"
              >
                <MessageCircle className="h-6 w-6 text-white" />
              </button>
            </motion.div>
          )}
        </AnimatePresence>

        <AnimatePresence>
          {(embedMode || widgetOpen) && (
            <motion.div
              initial={{ opacity: 0, scale: 0.9, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.9, y: 20 }}
              transition={{ type: "spring", damping: 25, stiffness: 300 }}
              className={
                embedMode
                  ? "flex h-full w-full flex-col overflow-hidden bg-white"
                  : "flex flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl"
              }
              style={{
                width: embedMode
                  ? "100%"
                  : isMobile
                    ? "100vw"
                    : Math.min(widgetExpanded ? WIDGET_EXPANDED_WIDTH : widgetDefaultWidth, 600),
                height: embedMode
                  ? "100%"
                  : widgetMinimized
                    ? 56
                    : isMobile
                      ? "100vh"
                      : Math.min(
                          widgetExpanded ? WIDGET_EXPANDED_HEIGHT : widgetDefaultHeight,
                          typeof window !== "undefined" ? window.innerHeight - 48 : 700,
                        ),
                position: embedMode ? "relative" : isMobile ? "fixed" : "relative",
                top: embedMode ? undefined : isMobile ? 0 : undefined,
                left: embedMode ? undefined : isMobile ? 0 : undefined,
                right: embedMode ? undefined : isMobile ? 0 : undefined,
                bottom: embedMode ? undefined : isMobile ? 0 : undefined,
                borderRadius: embedMode ? 0 : isMobile ? 0 : undefined,
              }}
            >
              <div
                className="sticky top-0 z-[2] flex shrink-0 items-center justify-between px-4 py-3"
                style={{ backgroundColor: widgetBrandColor }}
              >
                <div className="flex items-center gap-2">
                  <div className="flex h-8 w-8 items-center justify-center rounded-full bg-white/20 text-sm font-bold text-white">
                    {widgetBotName[0] || "A"}
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold text-white">{widgetBotName}</h3>
                    <div className="flex items-center gap-1">
                      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-green-400" />
                      <span className="text-[10px] text-white/80">
                        {hotelCode} • {normalizePhaseId(phase) || phase}
                      </span>
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-0.5">
                  {!embedMode && (
                    <button
                      type="button"
                      onClick={() => setWidgetMinimized((v) => !v)}
                      className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                      title={widgetMinimized ? "Restore" : "Minimize"}
                    >
                      {widgetMinimized ? <Plus className="h-4 w-4" /> : <Minus className="h-4 w-4" />}
                    </button>
                  )}
                  {!embedMode && !widgetMinimized && !isMobile && (
                    <button
                      type="button"
                      onClick={() => setWidgetExpanded((v) => !v)}
                      className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                      title={widgetExpanded ? "Shrink" : "Expand"}
                    >
                      <Maximize2 className="h-4 w-4" />
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      if (embedMode) {
                        postEmbedEvent("widget:close");
                        return;
                      }
                      setWidgetOpen(false);
                    }}
                    className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                    title={embedMode ? "Hide" : "Close"}
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              </div>

              {!widgetMinimized && (
                <div className="flex min-h-0 flex-1 flex-col">
            <div
              ref={messagesScrollRef}
              className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-3"
              style={{ backgroundColor: widgetBackgroundColor, color: widgetTextColor }}
            >
              {messages.length === 0 ? (
                <div className="rounded border border-dashed border-slate-300 bg-white p-3 text-sm" style={{ color: widgetTextColor }}>
                  {embedMode ? "Start a conversation." : "Start a conversation to test backend behavior in widget mode."}
                </div>
              ) : null}

              <div className="space-y-2">
                {messages.map((row) => {
                  const isAssistant = row.role === "assistant";
                  const isInlineTarget = inlineForm && inlineForm.messageId === row.id;
                  return (
                    <div key={row.id} className={isAssistant ? "flex justify-start" : "flex justify-end"}>
                      <div
                        className={`max-w-[86%] rounded-2xl px-3.5 py-2 text-sm whitespace-pre-wrap shadow-sm ${
                          isAssistant
                            ? "rounded-bl-sm border border-slate-200 bg-white text-slate-900"
                            : "rounded-br-sm text-white"
                        }`}
                        style={isAssistant ? { color: widgetTextColor } : { backgroundColor: widgetUiColor, color: "#ffffff" }}
                      >
                        {isAssistant ? (
                          <p
                            className="leading-relaxed"
                            dangerouslySetInnerHTML={{ __html: renderAssistantMarkdownToHtml(row.content) }}
                          />
                        ) : (
                          <p>{row.content}</p>
                        )}
                        {isAssistant &&
                        (row.raw?.service_llm_label ||
                          String((row.raw?.metadata || {}).service_llm_label || "").trim()) ? (
                          <p className="mt-1 text-[11px] text-slate-500">
                            answered by:{" "}
                            {String(
                              row.raw?.service_llm_label ||
                                String((row.raw?.metadata || {}).service_llm_label || "").trim(),
                            )}
                          </p>
                        ) : null}
                        {isInlineTarget ? renderInlineForm() : null}
                      </div>
                    </div>
                  );
                })}
                {isSending ? (
                  <div className="flex justify-start">
                    <div className="flex items-center gap-1.5 rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-3.5 py-2.5 shadow-sm">
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:-0.3s]" />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:-0.15s]" />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400" />
                    </div>
                  </div>
                ) : null}
              </div>

              {inlineForm && !inlineForm.successMessage ? null : suggestions.length > 0 ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {suggestions.map((item, idx) => (
                    <button
                      key={`${item}_${idx}`}
                      type="button"
                      onClick={() => void sendMessage(item, "suggested_action")}
                      className="rounded-full border bg-white px-3 py-1 text-xs transition-colors hover:text-white"
                      style={{
                        borderColor: `${widgetUiColor}40`,
                        color: widgetUiColor,
                      }}
                      onMouseEnter={(e) => {
                        (e.currentTarget as HTMLButtonElement).style.backgroundColor = widgetUiColor;
                      }}
                      onMouseLeave={(e) => {
                        (e.currentTarget as HTMLButtonElement).style.backgroundColor = "white";
                      }}
                    >
                      {item}
                    </button>
                  ))}
                </div>
              ) : null}
              <div ref={messageEndRef} />
            </div>

            <div className="shrink-0 border-t p-3" style={{ backgroundColor: widgetBackgroundColor, borderTopColor: `${widgetUiColor}33` }}>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void sendMessage(input, "typed_input");
                }}
                className="flex items-end gap-2"
              >
                <textarea
                  ref={inputRef}
                  rows={1}
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
                    event.preventDefault();
                    if (isSending || !input.trim()) return;
                    void sendMessage(input, "typed_input");
                  }}
                  placeholder="Type a message..."
                  className="max-h-24 min-h-[38px] flex-1 resize-none rounded border border-slate-300 px-2 py-2 text-sm"
                  style={{ borderColor: `${widgetUiColor}66`, color: widgetTextColor, backgroundColor: "#ffffff" }}
                />
                <button
                  type="submit"
                  disabled={isSending || !input.trim()}
                  aria-label="Send message"
                  className="flex h-10 w-10 items-center justify-center rounded-full text-white shadow-sm transition-opacity disabled:opacity-50"
                  style={{ backgroundColor: widgetUiColor }}
                >
                  <Send className="h-4 w-4" />
                </button>
              </form>
            </div>
                </div>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {historyOpen ? (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4">
          <div className="max-h-[80vh] w-full max-w-2xl overflow-auto rounded-lg bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold">Session History</h2>
              <button type="button" onClick={() => setHistoryOpen(false)} className="rounded border px-2 py-1 text-sm">
                Close
              </button>
            </div>
            {historyLoading ? <p className="text-sm text-slate-600">Loading...</p> : null}
            {historyError ? <p className="text-sm text-rose-600">{historyError}</p> : null}
            {historyData ? (
              <div className="space-y-2 text-sm">
                <div className="rounded bg-slate-50 p-2">
                  <p>
                    <span className="font-medium">Session:</span> {historyData.session_id}
                  </p>
                  <p>
                    <span className="font-medium">State:</span> {historyData.state}
                  </p>
                  <p>
                    <span className="font-medium">Hotel:</span> {historyData.hotel_code}
                  </p>
                  <p>
                    <span className="font-medium">Created:</span> {new Date(historyData.created_at).toLocaleString()}
                  </p>
                </div>
                {(historyData.messages || []).map((row, index) => (
                  <div key={`${row.timestamp}_${index}`} className="rounded border border-slate-200 p-2">
                    <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{row.role}</p>
                    <p className="mt-1 whitespace-pre-wrap">{row.content}</p>
                    <p className="mt-1 text-xs text-slate-500">{new Date(row.timestamp).toLocaleString()}</p>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {ticketsOpen ? (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4">
          <div className="max-h-[80vh] w-full max-w-3xl overflow-auto rounded-lg bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold">Local Tickets</h2>
              <button type="button" onClick={() => setTicketsOpen(false)} className="rounded border px-2 py-1 text-sm">
                Close
              </button>
            </div>
            {ticketsLoading ? <p className="text-sm text-slate-600">Loading...</p> : null}
            {ticketsError ? <p className="text-sm text-rose-600">{ticketsError}</p> : null}
            {!ticketsLoading && !ticketsError && tickets.length === 0 ? (
              <p className="text-sm text-slate-600">No tickets yet.</p>
            ) : null}
            <div className="space-y-2">
              {tickets.map((row, index) => (
                <div key={`${row.ticket_id || row.id || index}`} className="rounded border border-slate-200 p-3 text-sm">
                  <div className="mb-1 flex items-center justify-between">
                    <strong>{String(row.ticket_id || row.id || "-")}</strong>
                    <span className="rounded bg-slate-900 px-2 py-0.5 text-xs text-white">{String(row.status || "open")}</span>
                  </div>
                  <pre className="max-h-36 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 text-xs text-slate-700">
                    {JSON.stringify(row, null, 2)}
                  </pre>
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : null}

      {bookingModalOpen ? (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-lg rounded-lg bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold">Create Test Booking</h2>
              <button type="button" onClick={() => setBookingModalOpen(false)} className="rounded border px-2 py-1 text-sm">
                Close
              </button>
            </div>
            <form onSubmit={handleCreateBooking} className="grid gap-2">
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Guest Phone *</span>
                <input
                  required
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.guest_phone}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, guest_phone: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Guest Name</span>
                <input
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.guest_name}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, guest_name: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Property Name</span>
                <input
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.property_name}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, property_name: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Room Number</span>
                <input
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.room_number}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, room_number: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Room Type</span>
                <input
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.room_type}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, room_type: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Check-in *</span>
                <input
                  required
                  type="date"
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.check_in_date}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, check_in_date: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Check-out *</span>
                <input
                  required
                  type="date"
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.check_out_date}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, check_out_date: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Guests</span>
                <input
                  min={1}
                  type="number"
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.num_guests}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, num_guests: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Status</span>
                <select
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.status}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, status: event.target.value }))}
                >
                  <option value="reserved">Reserved</option>
                  <option value="checked_in">Checked In</option>
                  <option value="checked_out">Checked Out</option>
                </select>
              </label>
              {bookingDraftError ? <p className="text-sm text-rose-600">{bookingDraftError}</p> : null}
              <div>
                <button
                  type="submit"
                  disabled={bookingDraftSubmitting}
                  className="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {bookingDraftSubmitting ? "Creating..." : "Create Booking"}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
};

export default ChatHarness;
