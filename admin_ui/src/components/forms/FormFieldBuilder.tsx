import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Plus, Trash2, GripVertical, Phone, Star, ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

export interface FormField {
  id: string;
  label: string;
  type: string;
  required: boolean;
  countryCode?: boolean;
  selectedCountryCode?: string;
  options?: string[];
  ratingMax?: number;
}

interface FormFieldBuilderProps {
  fields: FormField[];
  onFieldsChange: (fields: FormField[]) => void;
}

const COUNTRY_CODES = ["+1", "+44", "+91", "+971", "+61", "+81", "+86", "+49", "+33", "+55"];
const RATING_SCALES = [3, 5, 7, 10];

const FormFieldBuilder = ({ fields, onFieldsChange }: FormFieldBuilderProps) => {
  const [expandedField, setExpandedField] = useState<string | null>(null);

  const addField = () => {
    onFieldsChange([...fields, { id: `f-${Date.now()}`, label: "New Field", type: "text", required: false }]);
  };

  const removeField = (id: string) => {
    onFieldsChange(fields.filter((f) => f.id !== id));
  };

  const updateField = (id: string, updates: Partial<FormField>) => {
    onFieldsChange(fields.map((f) => (f.id === id ? { ...f, ...updates } : f)));
  };

  const handleTypeChange = (id: string, newType: string) => {
    const updates: Partial<FormField> = { type: newType };
    if (newType === "select") {
      const field = fields.find((f) => f.id === id);
      if (!field?.options?.length) {
        updates.options = ["Option 1", "Option 2", "Option 3"];
      }
      setExpandedField(id);
    } else if (newType === "rating") {
      const field = fields.find((f) => f.id === id);
      if (!field?.ratingMax) {
        updates.ratingMax = 5;
      }
      setExpandedField(id);
    }
    updateField(id, updates);
  };

  const updateOption = (fieldId: string, index: number, value: string) => {
    const field = fields.find((f) => f.id === fieldId);
    if (!field?.options) return;
    const newOptions = [...field.options];
    newOptions[index] = value;
    updateField(fieldId, { options: newOptions });
  };

  const addOption = (fieldId: string) => {
    const field = fields.find((f) => f.id === fieldId);
    const options = field?.options ?? [];
    updateField(fieldId, { options: [...options, `Option ${options.length + 1}`] });
  };

  const removeOption = (fieldId: string, index: number) => {
    const field = fields.find((f) => f.id === fieldId);
    if (!field?.options) return;
    updateField(fieldId, { options: field.options.filter((_, i) => i !== index) });
  };

  const needsConfig = (type: string) => type === "select" || type === "rating";

  return (
    <div className="space-y-3">
      {fields.map((field) => (
        <div key={field.id} className="rounded-lg border">
          <div className="flex items-center gap-2 p-3">
            <GripVertical className="h-4 w-4 shrink-0 cursor-grab text-muted-foreground" />
            <Input
              value={field.label}
              onChange={(e) => updateField(field.id, { label: e.target.value })}
              className="h-8 flex-1 text-sm"
            />
            <Select value={field.type} onValueChange={(v) => handleTypeChange(field.id, v)}>
              <SelectTrigger className="h-8 w-28 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="text">Text</SelectItem>
                <SelectItem value="email">Email</SelectItem>
                <SelectItem value="tel">Phone</SelectItem>
                <SelectItem value="textarea">Textarea</SelectItem>
                <SelectItem value="select">Dropdown</SelectItem>
                <SelectItem value="rating">Rating</SelectItem>
              </SelectContent>
            </Select>
            {field.type === "tel" && (
              <div className="flex items-center gap-1">
                <Switch
                  checked={field.countryCode ?? false}
                  onCheckedChange={(v) => updateField(field.id, { countryCode: v })}
                />
                <Phone className="h-3 w-3 text-muted-foreground" />
              </div>
            )}
            {field.type === "tel" && field.countryCode && (
              <Select
                value={field.selectedCountryCode ?? "+91"}
                onValueChange={(v) => updateField(field.id, { selectedCountryCode: v })}
              >
                <SelectTrigger className="h-8 w-24 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {COUNTRY_CODES.map((code) => (
                    <SelectItem key={code} value={code}>{code}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            {needsConfig(field.type) && (
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={() => setExpandedField(expandedField === field.id ? null : field.id)}
              >
                {expandedField === field.id ? (
                  <ChevronDown className="h-3.5 w-3.5" />
                ) : (
                  <ChevronRight className="h-3.5 w-3.5" />
                )}
              </Button>
            )}
            <div className="flex items-center gap-1">
              <Switch
                checked={field.required}
                onCheckedChange={(v) => updateField(field.id, { required: v })}
              />
              <span className="text-[10px] text-muted-foreground">Req</span>
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-muted-foreground hover:text-destructive"
              onClick={() => removeField(field.id)}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>

          {/* Dropdown options editor */}
          {field.type === "select" && expandedField === field.id && (
            <div className="border-t bg-muted/30 p-3 space-y-2">
              <span className="text-xs font-medium text-muted-foreground">Dropdown Options</span>
              {(field.options ?? []).map((opt, i) => (
                <div key={i} className="flex items-center gap-2">
                  <span className="text-xs text-muted-foreground w-5 text-right">{i + 1}.</span>
                  <Input
                    value={opt}
                    onChange={(e) => updateOption(field.id, i, e.target.value)}
                    className="h-7 flex-1 text-xs"
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 text-muted-foreground hover:text-destructive"
                    onClick={() => removeOption(field.id, i)}
                  >
                    <Trash2 className="h-3 w-3" />
                  </Button>
                </div>
              ))}
              <Button variant="outline" size="sm" onClick={() => addOption(field.id)} className="h-7 text-xs w-full">
                <Plus className="mr-1 h-3 w-3" /> Add Option
              </Button>
            </div>
          )}

          {/* Rating scale editor */}
          {field.type === "rating" && expandedField === field.id && (
            <div className="border-t bg-muted/30 p-3 space-y-2">
              <span className="text-xs font-medium text-muted-foreground">Rating Scale</span>
              <div className="flex items-center gap-3">
                <Select
                  value={String(field.ratingMax ?? 5)}
                  onValueChange={(v) => updateField(field.id, { ratingMax: Number(v) })}
                >
                  <SelectTrigger className="h-7 w-20 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {RATING_SCALES.map((n) => (
                      <SelectItem key={n} value={String(n)}>1–{n}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <div className="flex items-center gap-0.5">
                  {Array.from({ length: field.ratingMax ?? 5 }).map((_, i) => (
                    <Star key={i} className="h-4 w-4 fill-yellow-400 text-yellow-400" />
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      ))}
      <Button variant="outline" size="sm" onClick={addField} className="w-full">
        <Plus className="mr-2 h-4 w-4" /> Add Field
      </Button>
    </div>
  );
};

export default FormFieldBuilder;
