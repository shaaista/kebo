import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Save, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { adminGet, adminSend } from "@/lib/adminApi";

interface Service {
  id: string;
  name: string;
  description: string;
  type: string;
  isActive: boolean;
  phaseId: string;
  ticketingEnabled: boolean;
  ticketingMode: string;
  ticketingPolicy: string;
  raw: Record<string, unknown>;
}

interface PhaseOption {
  id: string;
  name: string;
}

interface ServicesTabProps {
  propertyCode: string;
}

const ServicesTab = ({ propertyCode }: ServicesTabProps) => {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [services, setServices] = useState<Service[]>([]);
  const [phases, setPhases] = useState<PhaseOption[]>([]);

  const phaseMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of phases) {
      map.set(row.id, row.name || row.id);
    }
    return map;
  }, [phases]);

  const loadServices = async () => {
    setLoading(true);
    try {
      const [serviceRows, phaseRows] = await Promise.all([
        adminGet<Array<Record<string, unknown>>>("/config/services", propertyCode),
        adminGet<Array<Record<string, unknown>>>("/config/phases", propertyCode).catch(() => []),
      ]);

      setPhases(
        (Array.isArray(phaseRows) ? phaseRows : [])
          .map((row) => ({
            id: String(row.id || "").trim(),
            name: String(row.name || row.id || "").trim(),
          }))
          .filter((row) => row.id),
      );

      setServices(
        (Array.isArray(serviceRows) ? serviceRows : []).map((row, index) => {
          const source = row && typeof row === "object" ? (row as Record<string, unknown>) : {};
          return {
            id: String(source.id || `service_${index + 1}`),
            name: String(source.name || ""),
            description: String(source.description || ""),
            type: String(source.type || "service"),
            isActive: source.is_active !== false,
            phaseId: String(source.phase_id || ""),
            ticketingEnabled: Boolean(source.ticketing_enabled),
            ticketingMode: String(source.ticketing_mode || ""),
            ticketingPolicy: String(source.ticketing_policy || ""),
            raw: source,
          };
        }),
      );
    } catch (error) {
      toast({
        title: "Failed to load services",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadServices();
  }, [propertyCode]);

  const updateService = (id: string, field: keyof Service, value: string | boolean | Record<string, unknown>) =>
    setServices((prev) => prev.map((svc) => (svc.id === id ? { ...svc, [field]: value } : svc)));

  const handleSave = async () => {
    setSaving(true);
    try {
      for (const svc of services) {
        if (!svc.id.trim()) continue;
        const payload: Record<string, unknown> = {
          ...svc.raw,
          name: svc.name.trim(),
          type: svc.type.trim() || "service",
          description: svc.description.trim(),
          is_active: svc.isActive,
          phase_id: svc.phaseId.trim(),
          ticketing_enabled: svc.ticketingEnabled,
          ticketing_mode: svc.ticketingMode.trim(),
          ticketing_policy: svc.ticketingPolicy.trim(),
        };
        await adminSend("PUT", `/config/services/${encodeURIComponent(svc.id)}`, payload, propertyCode);
      }

      await loadServices();
      toast({ title: "Services saved" });
    } catch (error) {
      toast({
        title: "Failed to save services",
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
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading services...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <p className="text-sm text-muted-foreground">
          Edit existing services with full details, including phase mapping. New service creation is handled from phase flows.
        </p>
      </div>

      {services.length === 0 ? (
        <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">No services found for this property.</div>
      ) : (
        <div className="space-y-4">
          {services.map((svc) => {
            const phaseLabel = svc.phaseId ? phaseMap.get(svc.phaseId) || svc.phaseId : "Unassigned";
            const formConfig = svc.raw.form_config;
            const formFields = Array.isArray((formConfig as Record<string, unknown> | undefined)?.fields)
              ? (((formConfig as Record<string, unknown>).fields || []) as Array<Record<string, unknown>>)
              : [];
            const snapshot: Record<string, unknown> = {
              ...svc.raw,
              id: svc.id,
              name: svc.name,
              type: svc.type,
              description: svc.description,
              is_active: svc.isActive,
              phase_id: svc.phaseId,
              ticketing_enabled: svc.ticketingEnabled,
              ticketing_mode: svc.ticketingMode,
              ticketing_policy: svc.ticketingPolicy,
            };

            return (
              <Card key={svc.id}>
                <CardContent className="space-y-4 pt-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold">{svc.name || svc.id}</span>
                    <Badge variant="outline">{svc.id}</Badge>
                    <Badge variant={svc.isActive ? "default" : "secondary"}>{svc.isActive ? "Active" : "Inactive"}</Badge>
                    <Badge variant="outline">Phase: {phaseLabel}</Badge>
                  </div>

                  <div className="space-y-3">
                    <div className="space-y-1">
                      <p className="text-xs font-medium text-muted-foreground">Service Name</p>
                      <Input value={svc.name} onChange={(e) => updateService(svc.id, "name", e.target.value)} />
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium text-muted-foreground">Type</p>
                      <Input value={svc.type} onChange={(e) => updateService(svc.id, "type", e.target.value)} />
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium text-muted-foreground">Phase</p>
                      <select
                        className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                        value={svc.phaseId}
                        onChange={(e) => updateService(svc.id, "phaseId", e.target.value)}
                      >
                        <option value="">Select phase...</option>
                        {phases.map((phase) => (
                          <option key={phase.id} value={phase.id}>
                            {phase.name}
                          </option>
                        ))}
                        {svc.phaseId && !phases.some((phase) => phase.id === svc.phaseId) ? (
                          <option value={svc.phaseId}>{svc.phaseId}</option>
                        ) : null}
                      </select>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium text-muted-foreground">Description</p>
                      <Textarea
                        value={svc.description}
                        onChange={(e) => updateService(svc.id, "description", e.target.value)}
                        rows={2}
                      />
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium text-muted-foreground">Ticketing Mode</p>
                      <Input
                        value={svc.ticketingMode}
                        onChange={(e) => updateService(svc.id, "ticketingMode", e.target.value)}
                        placeholder="form / manual / auto"
                      />
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium text-muted-foreground">Ticketing Policy</p>
                      <Textarea
                        value={svc.ticketingPolicy}
                        onChange={(e) => updateService(svc.id, "ticketingPolicy", e.target.value)}
                        rows={3}
                      />
                    </div>
                    <label className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={svc.isActive}
                        onChange={(e) => updateService(svc.id, "isActive", e.target.checked)}
                      />
                      Active
                    </label>
                    <label className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={svc.ticketingEnabled}
                        onChange={(e) => updateService(svc.id, "ticketingEnabled", e.target.checked)}
                      />
                      Ticketing Enabled
                    </label>
                  </div>

                  {formFields.length > 0 ? (
                    <div className="rounded-md border bg-muted/30 p-3">
                      <p className="text-xs font-semibold text-muted-foreground">Form Fields ({formFields.length})</p>
                      <div className="mt-2 space-y-1 text-xs text-muted-foreground">
                        {formFields.map((field, index) => (
                          <p key={`${svc.id}_field_${index}`}>
                            {String(field.label || field.id || `field_${index + 1}`)} ({String(field.type || "text")})
                          </p>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  <details className="rounded-md border bg-muted/30 p-3">
                    <summary className="cursor-pointer text-xs font-semibold text-muted-foreground">
                      All Service Data (JSON)
                    </summary>
                    <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-background p-2 text-xs">
                      {JSON.stringify(snapshot, null, 2)}
                    </pre>
                  </details>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <Button onClick={handleSave} disabled={saving}>
        {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
        Save Services
      </Button>
    </div>
  );
};

export default ServicesTab;
