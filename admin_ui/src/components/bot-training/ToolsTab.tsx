import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Loader2, RefreshCw, Wrench } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { adminGet, adminSend } from "@/lib/adminApi";

interface ToolConfig {
  id: string;
  name: string;
  description: string;
  type: string;
  handler: string;
  channels: string[];
  enabled: boolean;
  requires_confirmation: boolean;
}

interface ToolsTabProps {
  propertyCode: string;
}

const ToolsTab = ({ propertyCode }: ToolsTabProps) => {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [savingId, setSavingId] = useState("");
  const [tools, setTools] = useState<ToolConfig[]>([]);

  const sortedTools = useMemo(
    () =>
      [...tools].sort((a, b) =>
        String(a.name || a.id)
          .toLowerCase()
          .localeCompare(String(b.name || b.id).toLowerCase()),
      ),
    [tools],
  );

  const loadTools = async () => {
    setLoading(true);
    try {
      const rows = await adminGet<Array<Record<string, unknown>>>("/config/tools", propertyCode);
      const parsed = (Array.isArray(rows) ? rows : [])
        .map((row) => {
          const id = String(row.id || "").trim();
          if (!id) return null;
          const channels = Array.isArray(row.channels)
            ? row.channels.map((entry) => String(entry || "").trim()).filter(Boolean)
            : [];
          return {
            id,
            name: String(row.name || id).trim(),
            description: String(row.description || "").trim(),
            type: String(row.type || "workflow").trim(),
            handler: String(row.handler || "").trim(),
            channels,
            enabled: Boolean(row.enabled !== false),
            requires_confirmation: Boolean(row.requires_confirmation),
          } satisfies ToolConfig;
        })
        .filter((item): item is ToolConfig => Boolean(item));
      setTools(parsed);
    } catch (error) {
      toast({
        title: "Failed to load tools",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadTools();
  }, [propertyCode]);

  const toggleTool = async (tool: ToolConfig, nextEnabled: boolean) => {
    setSavingId(tool.id);
    const previous = tools;
    setTools((prev) => prev.map((entry) => (entry.id === tool.id ? { ...entry, enabled: nextEnabled } : entry)));
    try {
      await adminSend(
        "PUT",
        `/config/tools/${encodeURIComponent(tool.id)}`,
        { enabled: nextEnabled },
        propertyCode,
      );
      toast({ title: `${tool.name} ${nextEnabled ? "enabled" : "disabled"}` });
    } catch (error) {
      setTools(previous);
      toast({
        title: "Failed to update tool",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setSavingId("");
    }
  };

  if (loading) {
    return (
      <div className="flex items-center py-8 text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading tools...
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Wrench className="h-4 w-4" />
          Enable/disable runtime tools for this property.
        </p>
        <Button variant="outline" size="sm" onClick={() => void loadTools()} disabled={Boolean(savingId)}>
          <RefreshCw className="mr-2 h-4 w-4" /> Refresh
        </Button>
      </div>

      {sortedTools.length === 0 ? (
        <Card>
          <CardContent className="py-6 text-sm text-muted-foreground">
            No tools configured for this property yet.
          </CardContent>
        </Card>
      ) : (
        sortedTools.map((tool) => (
          <Card key={tool.id}>
            <CardHeader className="pb-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <CardTitle className="text-base">{tool.name}</CardTitle>
                  <p className="mt-1 text-xs text-muted-foreground">{tool.id}</p>
                </div>
                <div className="flex items-center gap-3">
                  <Badge variant={tool.enabled ? "default" : "secondary"}>
                    {tool.enabled ? "Enabled" : "Disabled"}
                  </Badge>
                  <Switch
                    checked={tool.enabled}
                    disabled={savingId === tool.id}
                    onCheckedChange={(checked) => void toggleTool(tool, Boolean(checked))}
                  />
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {tool.description ? <p className="text-muted-foreground">{tool.description}</p> : null}
              <div className="flex flex-wrap gap-2 text-xs">
                <Badge variant="outline">type: {tool.type || "workflow"}</Badge>
                {tool.handler ? <Badge variant="outline">handler: {tool.handler}</Badge> : null}
                {tool.requires_confirmation ? <Badge variant="outline">requires confirmation</Badge> : null}
                {tool.channels.map((channel) => (
                  <Badge key={`${tool.id}_${channel}`} variant="secondary">
                    {channel}
                  </Badge>
                ))}
              </div>
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
};

export default ToolsTab;

