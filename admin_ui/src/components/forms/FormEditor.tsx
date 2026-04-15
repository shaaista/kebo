import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Settings2 } from "lucide-react";
import FormFieldBuilder from "./FormFieldBuilder";
import type { FormConfig } from "./types";
import type { FormField } from "./FormFieldBuilder";

interface FormEditorProps {
  form: FormConfig;
  onUpdate: (updates: Partial<FormConfig>) => void;
}

function toSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

const FormEditor = ({ form, onUpdate }: FormEditorProps) => {
  const handleNameChange = (name: string) => {
    onUpdate({ name, triggerId: toSlug(name) });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <Settings2 className="h-5 w-5" /> Form Configuration
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label className="text-sm font-medium">Form Name</Label>
            <Input
              value={form.name}
              onChange={(e) => handleNameChange(e.target.value)}
              placeholder="e.g. Booking Request"
            />
          </div>
          <div className="space-y-2">
            <Label className="text-sm font-medium">Trigger ID</Label>
            <div className="flex h-10 items-center rounded-md border bg-muted/50 px-3">
              <Badge variant="secondary" className="font-mono text-xs">
                {form.triggerId || "auto-generated"}
              </Badge>
            </div>
            <p className="text-[11px] text-muted-foreground">Bot uses this ID to invoke the form</p>
          </div>
        </div>
        <div className="space-y-2">
          <Label className="text-sm font-medium">Trigger Condition</Label>
          <Textarea
            value={form.triggerCondition}
            onChange={(e) => onUpdate({ triggerCondition: e.target.value })}
            placeholder="Describe when the bot should present this form, e.g. 'When the user asks to make a reservation'"
            className="min-h-[60px] resize-none text-sm"
          />
          <p className="text-[11px] text-muted-foreground">Natural language instruction for the bot</p>
        </div>

        <div className="space-y-2">
          <Label className="text-sm font-medium">Fields</Label>
          <FormFieldBuilder
            fields={form.fields}
            onFieldsChange={(fields: FormField[]) => onUpdate({ fields })}
          />
        </div>
      </CardContent>
    </Card>
  );
};

export default FormEditor;
