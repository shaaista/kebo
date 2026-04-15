import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { Save, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { adminGet, adminSend } from "@/lib/adminApi";

interface EscalationTabProps {
  propertyCode: string;
}

const EscalationTab = ({ propertyCode }: EscalationTabProps) => {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [liveChatEnabled, setLiveChatEnabled] = useState(true);
  const [ticketEnabled, setTicketEnabled] = useState(true);
  const [emailEnabled, setEmailEnabled] = useState(false);
  const [escalationMessage, setEscalationMessage] = useState(
    "I understand your concern. Let me connect you with a team member who can help you better.",
  );
  const [confidenceThreshold, setConfidenceThreshold] = useState("0.4");
  const [maxClarifications, setMaxClarifications] = useState("3");

  const loadConfig = async () => {
    setLoading(true);
    try {
      const data = await adminGet<Record<string, unknown>>("/config/escalation", propertyCode);
      const modes = Array.isArray(data?.modes) ? data.modes.map((m) => String(m)) : [];
      setLiveChatEnabled(modes.includes("live_chat"));
      setTicketEnabled(modes.includes("ticket"));
      setEmailEnabled(modes.includes("email"));
      setConfidenceThreshold(String(data?.confidence_threshold ?? "0.4"));
      setMaxClarifications(String(data?.max_clarification_attempts ?? "3"));
      setEscalationMessage(
        String(
          data?.escalation_message ||
            "I understand your concern. Let me connect you with a team member who can help you better.",
        ),
      );
    } catch (error) {
      toast({
        title: "Failed to load escalation settings",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadConfig();
  }, [propertyCode]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const modes = [
        ...(liveChatEnabled ? ["live_chat"] : []),
        ...(ticketEnabled ? ["ticket"] : []),
        ...(emailEnabled ? ["email"] : []),
      ];
      await adminSend(
        "PUT",
        "/config/escalation",
        {
          confidence_threshold: Number(confidenceThreshold) || 0.4,
          max_clarification_attempts: Number(maxClarifications) || 3,
          escalation_message: escalationMessage,
          modes,
        },
        propertyCode,
      );
      toast({ title: "Escalation config saved" });
    } catch (error) {
      toast({
        title: "Failed to save escalation settings",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center py-8 text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading escalation settings...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Escalation Channels</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <Label>Live Chat Handoff</Label>
            <Switch checked={liveChatEnabled} onCheckedChange={setLiveChatEnabled} />
          </div>
          <div className="flex items-center justify-between">
            <Label>Ticket Creation</Label>
            <Switch checked={ticketEnabled} onCheckedChange={setTicketEnabled} />
          </div>
          <div className="flex items-center justify-between">
            <Label>Email Escalation</Label>
            <Switch checked={emailEnabled} onCheckedChange={setEmailEnabled} />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Escalation Rules</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label>Confidence Threshold</Label>
            <Input
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={confidenceThreshold}
              onChange={(e) => setConfidenceThreshold(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label>Max Clarification Attempts</Label>
            <Input
              type="number"
              min="1"
              max="10"
              value={maxClarifications}
              onChange={(e) => setMaxClarifications(e.target.value)}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Escalation Message</CardTitle>
        </CardHeader>
        <CardContent>
          <Textarea value={escalationMessage} onChange={(e) => setEscalationMessage(e.target.value)} rows={4} />
        </CardContent>
      </Card>

      <Button onClick={handleSave} disabled={saving}>
        {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
        Save Escalation
      </Button>
    </div>
  );
};

export default EscalationTab;
