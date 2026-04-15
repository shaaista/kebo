import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Globe, Play, Square, CheckCircle2, Loader2, Building2, Settings2, ChevronDown, X, Power } from "lucide-react";

type CrawlStatus = "idle" | "crawling" | "done";

interface DetectedProperty {
  name: string;
  urlPattern: string;
  pagesFound: number;
  selected: boolean;
}

const crawlResults = [
  { type: "Pages", count: 142, icon: "📄" },
  { type: "Images", count: 89, icon: "🖼️" },
  { type: "Videos", count: 7, icon: "🎥" },
  { type: "Files", count: 23, icon: "📁" },
];

const crawlLog = [
  { url: "/gateway-calicut/index.html", status: "success", time: "0.3s", size: "24 KB" },
  { url: "/gateway-calicut/rooms", status: "success", time: "0.5s", size: "18 KB" },
  { url: "/vivanta-goa/", status: "success", time: "0.8s", size: "32 KB" },
  { url: "/vivanta-goa/spa", status: "success", time: "0.4s", size: "12 KB" },
  { url: "/taj-malabar/", status: "success", time: "0.6s", size: "28 KB" },
  { url: "/taj-malabar/events", status: "success", time: "0.2s", size: "8 KB" },
  { url: "/taj-exotica-goa/dining", status: "success", time: "1.1s", size: "42 KB" },
  { url: "/vivanta-trivandrum/rooms", status: "success", time: "1.5s", size: "15 KB" },
];

const initialDetectedProperties: DetectedProperty[] = [
  { name: "The Gateway Hotel Calicut", urlPattern: "/gateway-calicut/*", pagesFound: 18, selected: true },
  { name: "Vivanta Goa", urlPattern: "/vivanta-goa/*", pagesFound: 22, selected: true },
  { name: "Taj Malabar Resort & Spa", urlPattern: "/taj-malabar/*", pagesFound: 15, selected: true },
  { name: "The Gateway Hotel Athwalines Surat", urlPattern: "/gateway-surat/*", pagesFound: 12, selected: true },
  { name: "Taj Exotica Resort & Spa Goa", urlPattern: "/taj-exotica-goa/*", pagesFound: 20, selected: true },
  { name: "Vivanta Trivandrum", urlPattern: "/vivanta-trivandrum/*", pagesFound: 14, selected: true },
];

const WebCrawl = () => {
  const [url, setUrl] = useState("https://www.khil.com");
  const [status, setStatus] = useState<CrawlStatus>("idle");
  const [progress, setProgress] = useState(0);
  const [detectedProperties, setDetectedProperties] = useState<DetectedProperty[]>(initialDetectedProperties);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [projectSettingsOpen, setProjectSettingsOpen] = useState(false);
  const [projectName, setProjectName] = useState("Grand Hotel Bot");
  const [botEnabled, setBotEnabled] = useState(true);
  const [language, setLanguage] = useState("en");
  const [crawlDepth, setCrawlDepth] = useState("full");
  const [specificUrls, setSpecificUrls] = useState<string[]>([]);
  const [newSpecificUrl, setNewSpecificUrl] = useState("");
  const [autoSync, setAutoSync] = useState(false);
  const [syncFrequency, setSyncFrequency] = useState("weekly");

  const startCrawl = () => {
    setStatus("crawling");
    setProgress(0);
    const interval = setInterval(() => {
      setProgress((p) => {
        if (p >= 100) {
          clearInterval(interval);
          setStatus("done");
          return 100;
        }
        return p + 8;
      });
    }, 200);
  };

  const toggleProperty = (name: string) => {
    setDetectedProperties((prev) =>
      prev.map((p) => (p.name === name ? { ...p, selected: !p.selected } : p))
    );
  };

  const selectAllProperties = (selected: boolean) => {
    setDetectedProperties((prev) => prev.map((p) => ({ ...p, selected })));
  };

  const addSpecificUrl = () => {
    const trimmed = newSpecificUrl.trim();
    if (trimmed && !specificUrls.includes(trimmed)) {
      setSpecificUrls([...specificUrls, trimmed]);
      setNewSpecificUrl("");
    }
  };

  const removeSpecificUrl = (url: string) => {
    setSpecificUrls(specificUrls.filter((u) => u !== url));
  };

  const handleUrlKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addSpecificUrl();
    }
  };

  const selectedCount = detectedProperties.filter((p) => p.selected).length;

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Web Crawling</h1>
        <p className="text-muted-foreground">Crawl your website to extract content for the knowledge base</p>
      </div>

      {/* Project Settings */}
      <Collapsible open={projectSettingsOpen} onOpenChange={setProjectSettingsOpen}>
        <Card>
          <CollapsibleTrigger asChild>
            <CardHeader className="cursor-pointer hover:bg-muted/50 transition-colors">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Power className="h-5 w-5 text-muted-foreground" />
                  <CardTitle className="text-lg">Project Settings</CardTitle>
                </div>
                <ChevronDown className={`h-4 w-4 text-muted-foreground transition-transform ${projectSettingsOpen ? "rotate-180" : ""}`} />
              </div>
            </CardHeader>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <CardContent className="space-y-4 pt-0">
              <div className="space-y-2">
                <Label htmlFor="project-name">Project Name</Label>
                <Input id="project-name" value={projectName} onChange={(e) => setProjectName(e.target.value)} />
              </div>
              <div className="flex items-center justify-between">
                <div>
                  <Label>Bot Enabled</Label>
                  <p className="text-sm text-muted-foreground">Toggle the bot on/off on your website</p>
                </div>
                <Switch checked={botEnabled} onCheckedChange={setBotEnabled} />
              </div>
              <div className="space-y-2">
                <Label>Language</Label>
                <Select value={language} onValueChange={setLanguage}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="en">English</SelectItem>
                    <SelectItem value="ar">Arabic</SelectItem>
                    <SelectItem value="es">Spanish</SelectItem>
                    <SelectItem value="fr">French</SelectItem>
                    <SelectItem value="de">German</SelectItem>
                    <SelectItem value="zh">Chinese</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </CardContent>
          </CollapsibleContent>
        </Card>
      </Collapsible>

      {/* Crawl Settings */}
      <Collapsible open={settingsOpen} onOpenChange={setSettingsOpen}>
        <Card>
          <CollapsibleTrigger asChild>
            <CardHeader className="cursor-pointer hover:bg-muted/50 transition-colors">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Settings2 className="h-5 w-5 text-muted-foreground" />
                  <CardTitle className="text-lg">Crawl Settings</CardTitle>
                </div>
                <ChevronDown className={`h-4 w-4 text-muted-foreground transition-transform ${settingsOpen ? "rotate-180" : ""}`} />
              </div>
            </CardHeader>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <CardContent className="space-y-4 pt-0">

              <div className="space-y-3">
                <div>
                  <Label>Specific URLs</Label>
                  <p className="text-sm text-muted-foreground">Add specific pages you want the bot to crawl</p>
                </div>
                <div className="flex gap-2">
                  <Input
                    placeholder="https://example.com/page"
                    value={newSpecificUrl}
                    onChange={(e) => setNewSpecificUrl(e.target.value)}
                    onKeyDown={handleUrlKeyDown}
                  />
                  <Button onClick={addSpecificUrl} variant="outline" size="sm" className="shrink-0">
                    Add URL
                  </Button>
                </div>
                {specificUrls.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {specificUrls.map((sUrl) => (
                      <span
                        key={sUrl}
                        className="inline-flex items-center gap-1 rounded-full border border-border bg-muted px-3 py-1 text-sm"
                      >
                        {sUrl}
                        <button
                          onClick={() => removeSpecificUrl(sUrl)}
                          className="ml-1 rounded-full p-0.5 hover:bg-destructive/20 hover:text-destructive"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>Auto-sync</Label>
                    <p className="text-sm text-muted-foreground">Automatically re-crawl and update knowledge base</p>
                  </div>
                  <Switch checked={autoSync} onCheckedChange={setAutoSync} />
                </div>
                {autoSync && (
                  <div className="space-y-2">
                    <Label>Sync Frequency</Label>
                    <Select value={syncFrequency} onValueChange={setSyncFrequency}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="daily">Daily</SelectItem>
                        <SelectItem value="weekly">Weekly (Monday)</SelectItem>
                        <SelectItem value="monthly">Monthly (1st day)</SelectItem>
                        <SelectItem value="bimonthly">In 2 months</SelectItem>
                        <SelectItem value="quarterly">In 3 months</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                )}
              </div>
            </CardContent>
          </CollapsibleContent>
        </Card>
      </Collapsible>

      {/* Start Crawl */}
      <Card>
        <CardHeader>
          <CardTitle>Start a Crawl</CardTitle>
          <CardDescription>Enter your website URL to begin crawling</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex gap-3">
            <div className="relative flex-1">
              <Globe className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://yourwebsite.com"
                className="pl-10"
              />
            </div>
            {status === "crawling" ? (
              <Button variant="destructive" onClick={() => setStatus("idle")}>
                <Square className="mr-2 h-4 w-4" /> Stop
              </Button>
            ) : (
              <Button onClick={startCrawl}>
                <Play className="mr-2 h-4 w-4" /> Start Crawl
              </Button>
            )}
          </div>

          {status !== "idle" && (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <span className="flex items-center gap-2">
                  {status === "crawling" ? (
                    <Loader2 className="h-4 w-4 animate-spin text-primary" />
                  ) : (
                    <CheckCircle2 className="h-4 w-4 text-green-500" />
                  )}
                  {status === "crawling" ? "Crawling..." : "Crawl complete!"}
                </span>
                <span className="text-muted-foreground">{Math.min(progress, 100)}%</span>
              </div>
              <Progress value={Math.min(progress, 100)} />
            </div>
          )}
        </CardContent>
      </Card>

      {status === "done" && (
        <>
          <div className="grid gap-4 sm:grid-cols-4">
            {crawlResults.map((r) => (
              <Card key={r.type}>
                <CardContent className="flex items-center gap-3 p-4">
                  <span className="text-2xl">{r.icon}</span>
                  <div>
                    <div className="text-xl font-bold">{r.count}</div>
                    <div className="text-xs text-muted-foreground">{r.type}</div>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>

          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Building2 className="h-5 w-5 text-primary" />
                  <CardTitle className="text-lg">Detected Properties ({detectedProperties.length})</CardTitle>
                </div>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => selectAllProperties(true)}>
                    Select All
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => selectAllProperties(false)}>
                    Deselect All
                  </Button>
                </div>
              </div>
              <CardDescription>
                {selectedCount} of {detectedProperties.length} properties selected for content import
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="divide-y rounded-lg border">
                {detectedProperties.map((prop) => (
                  <div key={prop.name} className="flex items-center justify-between px-4 py-3">
                    <div className="flex items-center gap-3">
                      <Switch
                        checked={prop.selected}
                        onCheckedChange={() => toggleProperty(prop.name)}
                      />
                      <div>
                        <div className="text-sm font-medium">{prop.name}</div>
                        <div className="text-xs text-muted-foreground font-mono">{prop.urlPattern}</div>
                      </div>
                    </div>
                    <Badge variant="secondary">{prop.pagesFound} pages</Badge>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Crawl Log</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="divide-y">
                {crawlLog.map((log) => (
                  <div key={log.url} className="flex items-center justify-between py-2">
                    <div className="flex items-center gap-2">
                      <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
                      <span className="text-sm font-mono">{log.url}</span>
                    </div>
                    <div className="flex items-center gap-4">
                      <Badge variant="secondary" className="text-xs">{log.size}</Badge>
                      <span className="text-xs text-muted-foreground w-8 text-right">{log.time}</span>
                    </div>
                  </div>
                ))}
              </div>
              <Button variant="outline" className="mt-4 w-full" asChild>
                <a href="/app/content">Review All Content →</a>
              </Button>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
};

export default WebCrawl;
