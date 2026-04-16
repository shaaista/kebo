import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { Copy, Check, Eye } from "lucide-react";
import { toast } from "sonner";

interface ChannelsData {
  primaryColor: string;
  accentColor: string;
  bgColor: string;
  textColor: string;
  widgetPosition: string;
  widgetWidth: number;
  widgetHeight: number;
  industryFeatures: string;
  webEnabled: boolean;
  whatsappEnabled: boolean;
}

interface ChannelsStepProps {
  data: ChannelsData;
  propertyCode: string;
  botName: string;
  onChange: <K extends keyof ChannelsData>(field: K, value: ChannelsData[K]) => void;
  onSave: () => void;
}

const ChannelsStep = ({ data, propertyCode, botName, onChange, onSave }: ChannelsStepProps) => {
  const [copied, setCopied] = useState(false);
  const widgetHost = typeof window === "undefined" ? "https://your-widget-host.com" : window.location.origin;
  const normalizedPropertyCode = String(propertyCode || "default").trim().toLowerCase() || "default";
  const resolvedBotName = String(botName || "Assistant").trim() || "Assistant";
  const previewWidth = Math.min(Math.max(Number(data.widgetWidth) || 380, 280), 420);
  const previewHeight = Math.min(Math.max(Number(data.widgetHeight) || 600, 350), 560);

  const embedCode = `<script src="${widgetHost}/static/embed/kebo-widget-loader.js"
  data-widget-id="${normalizedPropertyCode}"
  data-hotel-code="${normalizedPropertyCode}"
  data-phase="pre_booking"
  data-brand-color="${data.primaryColor}"
  data-accent-color="${data.accentColor}"
  data-bg-color="${data.bgColor}"
  data-text-color="${data.textColor}"
  data-bot-name="${resolvedBotName}"
  data-position="${data.widgetPosition}"
  data-width="${data.widgetWidth}"
  data-height="${data.widgetHeight}">
</script>`;

  const handleCopy = () => {
    navigator.clipboard.writeText(embedCode);
    setCopied(true);
    toast.success("Embed code copied to clipboard!");
    setTimeout(() => setCopied(false), 2000);
  };

  return (
  <Card>
    <CardHeader className="rounded-t-lg bg-primary px-6 py-4">
      <div className="flex items-center justify-between">
        <CardTitle className="text-lg text-primary-foreground">Step 3: Channels, Colors & Features</CardTitle>
        <Button size="sm" variant="secondary" onClick={onSave}>Save</Button>
      </div>
    </CardHeader>
    <CardContent className="space-y-6 p-6">
      <div className="grid gap-4 lg:grid-cols-[1fr_auto] lg:items-start">
        <div>
          <Label className="mb-3 block">Brand Colors</Label>
          <div className="grid grid-cols-2 gap-4">
            {([
              { key: "primaryColor" as const, label: "Primary" },
              { key: "accentColor" as const, label: "Accent" },
              { key: "bgColor" as const, label: "Background" },
              { key: "textColor" as const, label: "Text" },
            ]).map(({ key, label }) => (
              <div key={key} className="space-y-1.5 rounded-md border bg-white p-2">
                <Label className="text-xs">{label}</Label>
                <div className="flex items-center gap-2">
                  <input
                    type="color"
                    value={data[key]}
                    onChange={(e) => onChange(key, e.target.value)}
                    className="h-9 w-12 cursor-pointer rounded border border-input bg-transparent"
                  />
                  <span className="text-xs text-muted-foreground">{data[key]}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="space-y-2 lg:w-[420px]">
          <Label className="mb-2 flex items-center gap-2">
            <Eye className="h-4 w-4" />
            Widget Preview
          </Label>
          <p className="text-xs text-muted-foreground">
            Live preview of current colors and size configuration.
          </p>
          <div className="rounded-xl border bg-slate-100 p-3">
            <div
              className="overflow-hidden rounded-2xl border border-slate-300 shadow-xl"
              style={{ width: previewWidth, height: previewHeight }}
            >
              <div
                className="flex items-center justify-between px-4 py-3"
                style={{ backgroundColor: data.primaryColor }}
              >
                <div>
                  <p className="text-sm font-semibold text-white">{resolvedBotName}</p>
                  <p className="text-[11px] text-white/80">{normalizedPropertyCode} • pre_booking</p>
                </div>
                <button
                  type="button"
                  className="rounded-md px-2 py-1 text-xs text-white/90"
                  style={{ backgroundColor: `${data.primaryColor}66` }}
                >
                  Close
                </button>
              </div>

              <div
                className="flex h-[calc(100%-56px)] flex-col"
                style={{ backgroundColor: data.bgColor, color: data.textColor }}
              >
                <div className="flex-1 space-y-2 overflow-auto p-3 text-sm">
                  <div className="max-w-[82%] rounded-2xl rounded-bl-sm bg-white px-3 py-2 shadow-sm">
                    Hi! How can I help you today?
                  </div>
                  <div className="flex justify-end">
                    <div
                      className="max-w-[72%] rounded-2xl rounded-br-sm px-3 py-2 text-white shadow-sm"
                      style={{ backgroundColor: data.primaryColor }}
                    >
                      Hi
                    </div>
                  </div>
                  <div className="max-w-[82%] rounded-2xl rounded-bl-sm bg-white px-3 py-2 shadow-sm text-slate-500">
                    ...
                  </div>
                </div>
                <div className="border-t border-slate-200 bg-white p-3">
                  <div className="flex items-center gap-2">
                    <input
                      readOnly
                      value="Type a message..."
                      className="h-10 flex-1 rounded-lg border px-3 text-sm"
                      style={{ borderColor: `${data.primaryColor}66`, color: data.textColor }}
                    />
                    <button
                      type="button"
                      className="flex h-10 w-10 items-center justify-center rounded-full text-white"
                      style={{ backgroundColor: data.primaryColor }}
                    >
                      →
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="space-y-1.5">
          <Label>Widget Position</Label>
          <Select value={data.widgetPosition} onValueChange={(v) => onChange("widgetPosition", v)}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="right">Bottom Right</SelectItem>
              <SelectItem value="left">Bottom Left</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Widget Width (px)</Label>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={280}
              max={600}
              step={10}
              value={data.widgetWidth}
              onChange={(e) => onChange("widgetWidth", Number(e.target.value))}
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            />
            <span className="text-xs text-muted-foreground whitespace-nowrap">280–600</span>
          </div>
        </div>
        <div className="space-y-1.5">
          <Label>Widget Height (px)</Label>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={350}
              max={800}
              step={10}
              value={data.widgetHeight}
              onChange={(e) => onChange("widgetHeight", Number(e.target.value))}
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            />
            <span className="text-xs text-muted-foreground whitespace-nowrap">350–800</span>
          </div>
        </div>
      </div>

      <div className="space-y-1.5">
        <Label>Industry Features</Label>
        <Textarea
          value={data.industryFeatures}
          readOnly
          rows={4}
          className="bg-muted/50 cursor-default focus-visible:ring-0"
          placeholder="Select an industry in Step 1 to auto-populate features"
        />
        <p className="text-xs text-muted-foreground">Auto-populated based on industry selection</p>
      </div>

      <div>
        <Label className="mb-3 block">Channel Enablement</Label>
        <div className="flex flex-wrap gap-6">
          <label className="flex items-center gap-2">
            <Checkbox checked={data.webEnabled} onCheckedChange={(v) => onChange("webEnabled", !!v)} />
            <span className="text-sm">Web Widget Enabled</span>
          </label>
          <label className="flex items-center gap-2">
            <Checkbox checked={data.whatsappEnabled} onCheckedChange={(v) => onChange("whatsappEnabled", !!v)} />
            <span className="text-sm">WhatsApp Enabled</span>
          </label>
        </div>
      </div>

      <div className="space-y-2">
        <Label className="mb-2 block">Embed Code</Label>
        <p className="text-xs text-muted-foreground mb-2">
          Copy and paste this code into your website's HTML to add the Kebo Bot widget.
        </p>
        <div className="relative">
          <pre className="rounded-lg border bg-muted/50 p-3 text-xs font-mono overflow-x-auto whitespace-pre-wrap">
            {embedCode}
          </pre>
          <Button
            size="sm"
            variant="outline"
            className="absolute right-2 top-2 h-7 gap-1"
            onClick={handleCopy}
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
      </div>
    </CardContent>
  </Card>
  );
};

export default ChannelsStep;
export type { ChannelsData };
