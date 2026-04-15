import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Plus, Trash2 } from "lucide-react";
import type { FormConfig } from "./types";
import { cn } from "@/lib/utils";

interface FormListProps {
  forms: FormConfig[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onAdd: () => void;
  onDelete: (id: string) => void;
  onToggle: (id: string, enabled: boolean) => void;
}

const FormList = ({ forms, selectedId, onSelect, onAdd, onDelete, onToggle }: FormListProps) => {
  return (
    <div className="space-y-2">
      {forms.map((form) => (
        <div
          key={form.id}
          onClick={() => onSelect(form.id)}
          className={cn(
            "cursor-pointer rounded-lg border p-3 transition-colors hover:bg-accent/50",
            selectedId === form.id && "border-primary bg-accent/50"
          )}
        >
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">{form.name}</span>
            <div className="flex items-center gap-2">
              <Switch
                checked={form.enabled}
                onCheckedChange={(v) => {
                  onToggle(form.id, v);
                }}
                onClick={(e) => e.stopPropagation()}
              />
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground hover:text-destructive"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(form.id);
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
          <div className="mt-1 flex items-center gap-2">
            <Badge variant="secondary" className="text-[10px] font-mono">
              {form.triggerId}
            </Badge>
            <span className="text-xs text-muted-foreground">{form.fields.length} fields</span>
          </div>
        </div>
      ))}
      <Button variant="outline" size="sm" onClick={onAdd} className="w-full">
        <Plus className="mr-2 h-4 w-4" /> New Form
      </Button>
    </div>
  );
};

export default FormList;
