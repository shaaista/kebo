const API_BASE = "/admin/api";
const PROPERTY_KEY = "nexoria_admin_property_code";

export function normalizePropertyCode(value: string): string {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

export function getActivePropertyCode(): string {
  if (typeof window === "undefined") return "default";
  const fromStorage = normalizePropertyCode(window.localStorage.getItem(PROPERTY_KEY) || "");
  return fromStorage || "default";
}

export function setActivePropertyCode(code: string): string {
  const normalized = normalizePropertyCode(code) || "default";
  if (typeof window !== "undefined") {
    window.localStorage.setItem(PROPERTY_KEY, normalized);
  }
  return normalized;
}

type FetchOptions = RequestInit & {
  propertyCode?: string;
};

async function parseResponse(response: Response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export async function adminFetch(path: string, options: FetchOptions = {}) {
  const resolvedProperty = normalizePropertyCode(options.propertyCode || getActivePropertyCode()) || "default";
  const headers = new Headers(options.headers || {});
  headers.set("x-hotel-code", resolvedProperty);
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  const payload = await parseResponse(response);
  if (!response.ok) {
    const detail =
      (payload && typeof payload === "object" && "detail" in payload && String((payload as { detail?: unknown }).detail || "").trim()) ||
      response.statusText ||
      `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

export async function adminGet<T = unknown>(path: string, propertyCode?: string): Promise<T> {
  return (await adminFetch(path, { method: "GET", propertyCode })) as T;
}

export async function adminSend<T = unknown>(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
  propertyCode?: string,
): Promise<T> {
  const headers = new Headers();
  let payloadBody: BodyInit | undefined;
  if (typeof FormData !== "undefined" && body instanceof FormData) {
    payloadBody = body;
  } else if (body !== undefined) {
    headers.set("Content-Type", "application/json");
    payloadBody = JSON.stringify(body);
  }
  return (await adminFetch(path, { method, body: payloadBody, headers, propertyCode })) as T;
}

export function splitLines(value: string): string[] {
  return String(value || "")
    .split("\n")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

export function joinLines(values: unknown): string {
  return Array.isArray(values) ? values.filter(Boolean).join("\n") : "";
}

export function makeServiceId(name: string): string {
  return String(name || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 64);
}
