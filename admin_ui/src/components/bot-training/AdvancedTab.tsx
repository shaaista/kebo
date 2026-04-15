import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Save, Download, Upload, Database, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { adminGet, adminSend } from "@/lib/adminApi";

interface AdvancedTabProps {
  propertyCode: string;
  onImported?: () => void;
}

const AdvancedTab = ({ propertyCode, onImported }: AdvancedTabProps) => {
  const { toast } = useToast();
  const [saving, setSaving] = useState(false);
  const [dbStatus, setDbStatus] = useState<Record<string, unknown>>({});
  const [rawConfig, setRawConfig] = useState("");

  const loadDbStatus = async () => {
    try {
      const payload = await adminGet<Record<string, unknown>>("/db/status", propertyCode);
      setDbStatus(payload || {});
    } catch (error) {
      toast({
        title: "Failed to load DB status",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    }
  };

  const syncDb = async () => {
    setSaving(true);
    try {
      await adminSend("POST", "/db/sync", {}, propertyCode);
      await loadDbStatus();
      toast({ title: "DB sync triggered" });
    } catch (error) {
      toast({
        title: "DB sync failed",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  const exportConfig = async () => {
    try {
      const payload = await adminGet<{ config_json?: string }>("/config/export", propertyCode);
      const raw = String(payload?.config_json || "");
      const blob = new Blob([raw], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "bot_config.json";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast({ title: "Config exported" });
    } catch (error) {
      toast({
        title: "Export failed",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    }
  };

  const importConfig = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json,application/json";
    input.onchange = async (event) => {
      const file = (event.target as HTMLInputElement).files?.[0];
      if (!file) return;
      const text = await file.text();
      setSaving(true);
      try {
        JSON.parse(text);
        await adminSend("POST", "/config/import", { config_json: text }, propertyCode);
        setRawConfig(text);
        onImported?.();
        toast({ title: "Config imported" });
      } catch (error) {
        toast({
          title: "Import failed",
          description: String(error instanceof Error ? error.message : error),
          variant: "destructive",
        });
      } finally {
        setSaving(false);
      }
    };
    input.click();
  };

  const saveRawConfig = async () => {
    setSaving(true);
    try {
      JSON.parse(rawConfig);
      await adminSend("POST", "/config/import", { config_json: rawConfig }, propertyCode);
      onImported?.();
      toast({ title: "Raw config saved" });
    } catch (error) {
      toast({
        title: "Failed to save raw config",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Configuration Utilities</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={exportConfig} disabled={saving}>
            <Download className="mr-2 h-4 w-4" /> Export JSON
          </Button>
          <Button variant="outline" onClick={importConfig} disabled={saving}>
            <Upload className="mr-2 h-4 w-4" /> Import JSON
          </Button>
          <Button variant="outline" onClick={loadDbStatus} disabled={saving}>
            <Database className="mr-2 h-4 w-4" /> DB Status
          </Button>
          <Button variant="outline" onClick={syncDb} disabled={saving}>
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
            Sync DB
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Database Status</CardTitle>
        </CardHeader>
        <CardContent>
          <pre className="max-h-80 overflow-auto rounded-md border bg-muted/40 p-3 text-xs">
            {JSON.stringify(dbStatus, null, 2)}
          </pre>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Raw Configuration (JSON)</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <Label>Paste full config JSON here and save</Label>
            <Textarea
              value={rawConfig}
              onChange={(e) => setRawConfig(e.target.value)}
              rows={14}
              className="font-mono text-xs"
            />
          </div>
          <Button onClick={saveRawConfig} disabled={saving || !rawConfig.trim()}>
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
            Save Raw Config
          </Button>
        </CardContent>
      </Card>
    </div>
  );
};

export default AdvancedTab;
