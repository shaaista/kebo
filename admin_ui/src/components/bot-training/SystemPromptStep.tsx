import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

interface PromptData {
  promptTemplate: string;
  responseStyle: string;
  systemPrompt: string;
  classifierPrompt: string;
}

interface SystemPromptStepProps {
  data: PromptData;
  onChange: (field: keyof PromptData, value: string) => void;
  onApplyTemplate: () => void;
  onSave: () => void;
}

const templates = [
  { value: "default", label: "Default Assistant" },
  { value: "hotel", label: "Hotel Concierge" },
  { value: "restaurant", label: "Restaurant Host" },
  { value: "support", label: "Customer Support" },
  { value: "sales", label: "Sales Assistant" },
];

const SystemPromptStep = ({ data, onChange, onApplyTemplate, onSave }: SystemPromptStepProps) => (
  <Card>
    <CardHeader className="rounded-t-lg bg-primary px-6 py-4">
      <div className="flex items-center justify-between">
        <CardTitle className="text-lg text-primary-foreground">Step 3: AI System Prompt & Behavior</CardTitle>
        <Button size="sm" variant="secondary" onClick={onSave}>Save</Button>
      </div>
    </CardHeader>
    <CardContent className="space-y-4 p-6">
      <div className="flex gap-3">
        <div className="flex-1 space-y-1.5">
          <Label>Prompt Template</Label>
          <Select value={data.promptTemplate} onValueChange={(v) => onChange("promptTemplate", v)}>
            <SelectTrigger><SelectValue placeholder="Choose a template" /></SelectTrigger>
            <SelectContent>
              {templates.map((t) => (
                <SelectItem key={t.value} value={t.value}>{t.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-end">
          <Button variant="outline" onClick={onApplyTemplate}>Apply</Button>
        </div>
      </div>
      <div className="space-y-1.5">
        <Label>Response Style</Label>
        <Input value={data.responseStyle} onChange={(e) => onChange("responseStyle", e.target.value)} placeholder="e.g. professional, concise, friendly" />
      </div>
      <div className="space-y-1.5">
        <Label>System Prompt</Label>
        <Textarea
          value={data.systemPrompt}
          onChange={(e) => onChange("systemPrompt", e.target.value)}
          placeholder={`You are {bot_name}, a virtual assistant for {business_name} located in {city}. You help guests with inquiries about rooms, services, and bookings...`}
          rows={8}
        />
        <p className="text-xs text-muted-foreground">Use variables: {"{bot_name}"}, {"{business_name}"}, {"{city}"}, {"{industry}"}</p>
      </div>
      <div className="space-y-1.5">
        <Label>Classifier Prompt (Optional)</Label>
        <Textarea
          value={data.classifierPrompt}
          onChange={(e) => onChange("classifierPrompt", e.target.value)}
          placeholder="Classify the user's intent into categories like booking, inquiry, complaint, feedback..."
          rows={4}
        />
      </div>
    </CardContent>
  </Card>
);

export default SystemPromptStep;
export type { PromptData };
