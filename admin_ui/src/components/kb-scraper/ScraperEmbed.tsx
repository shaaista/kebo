import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ExternalLink, RefreshCcw, Wifi, WifiOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type ConnectionState = "checking" | "online" | "offline";

interface ScraperEmbedProps {
  title: string;
  description: string;
  preferReview?: boolean;
}

const DEFAULT_SCRAPER_URL = "/kb-scraper";

function resolveScraperBaseUrl(): string {
  const raw = String(import.meta.env.VITE_KB_SCRAPER_URL || "").trim();
  if (!raw) return DEFAULT_SCRAPER_URL;

  try {
    return new URL(raw, window.location.origin).toString().replace(/\/+$/, "");
  } catch {
    return DEFAULT_SCRAPER_URL;
  }
}

function buildEmbedUrl(baseUrl: string, preferReview: boolean): string {
  try {
    const url = new URL(baseUrl);
    if (preferReview) {
      url.searchParams.set("screen", "review");
    }
    return url.toString();
  } catch {
    return baseUrl;
  }
}

export default function ScraperEmbed({
  title,
  description,
  preferReview = false,
}: ScraperEmbedProps) {
  const baseUrl = useMemo(resolveScraperBaseUrl, []);
  const iframeUrl = useMemo(() => buildEmbedUrl(baseUrl, preferReview), [baseUrl, preferReview]);

  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [reloadCounter, setReloadCounter] = useState(0);
  const [lastCheckedAt, setLastCheckedAt] = useState<string>("");

  useEffect(() => {
    let active = true;

    const checkHealth = async () => {
      setConnection("checking");
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 4000);

      try {
        const healthUrl = new URL("health", `${baseUrl}/`).toString();
        const response = await fetch(healthUrl, { signal: controller.signal });
        if (!active) return;
        setConnection(response.ok ? "online" : "offline");
      } catch {
        if (!active) return;
        setConnection("offline");
      } finally {
        window.clearTimeout(timeout);
        if (active) {
          setLastCheckedAt(new Date().toLocaleTimeString());
        }
      }
    };

    checkHealth();
    const interval = window.setInterval(checkHealth, 20000);

    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [baseUrl]);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="text-2xl font-bold">{title}</h1>
          <p className="text-muted-foreground">{description}</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={connection === "online" ? "default" : "secondary"} className="gap-1">
            {connection === "online" ? (
              <>
                <Wifi className="h-3.5 w-3.5" />
                Scraper Online
              </>
            ) : (
              <>
                <WifiOff className="h-3.5 w-3.5" />
                {connection === "checking" ? "Checking..." : "Scraper Offline"}
              </>
            )}
          </Badge>
          <Button variant="outline" size="sm" onClick={() => setReloadCounter((v) => v + 1)}>
            <RefreshCcw className="mr-2 h-4 w-4" />
            Reload
          </Button>
          <Button variant="outline" size="sm" onClick={() => window.open(iframeUrl, "_blank", "noopener,noreferrer")}>
            <ExternalLink className="mr-2 h-4 w-4" />
            Open
          </Button>
        </div>
      </div>

      {connection === "offline" ? (
        <Card className="border-amber-300/70 bg-amber-50/40">
          <CardContent className="flex items-start gap-3 p-4">
            <AlertTriangle className="mt-0.5 h-5 w-5 text-amber-600" />
            <div className="text-sm text-muted-foreground">
              <p className="font-medium text-foreground">KB scraper service is not reachable.</p>
              <p>
                Start it from `Kepsla-hotal-kb-scraper-main` (default URL: <code>{baseUrl}</code>) and this screen
                will auto-sync.
              </p>
              {lastCheckedAt ? <p className="mt-1 text-xs">Last check: {lastCheckedAt}</p> : null}
            </div>
          </CardContent>
        </Card>
      ) : null}

      <Card className="overflow-hidden">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">KB Scraper Frontend</CardTitle>
          <CardDescription>
            This embeds the original scraper UI so crawler sessions and review flows stay in sync with the bot admin.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <iframe
            key={`${iframeUrl}-${reloadCounter}`}
            src={iframeUrl}
            title="KB Scraper UI"
            className="h-[calc(100vh-18rem)] min-h-[680px] w-full border-0"
          />
        </CardContent>
      </Card>
    </div>
  );
}
