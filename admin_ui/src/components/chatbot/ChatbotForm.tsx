import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { format } from "date-fns";
import { CalendarIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { FormField, FEEDBACK_RATINGS, FEEDBACK_TAGS } from "./types";

const COUNTRY_CODES = [
  { code: "+91", country: "IN", flag: "🇮🇳" },
  { code: "+1", country: "US", flag: "🇺🇸" },
  { code: "+44", country: "UK", flag: "🇬🇧" },
  { code: "+971", country: "AE", flag: "🇦🇪" },
  { code: "+65", country: "SG", flag: "🇸🇬" },
  { code: "+61", country: "AU", flag: "🇦🇺" },
  { code: "+49", country: "DE", flag: "🇩🇪" },
  { code: "+33", country: "FR", flag: "🇫🇷" },
  { code: "+81", country: "JP", flag: "🇯🇵" },
  { code: "+86", country: "CN", flag: "🇨🇳" },
];

interface ChatbotFormProps {
  fields: FormField[];
  onSubmit: (data: Record<string, any>) => void;
  brandColor: string;
  isSubmitted?: boolean;
}

export function ChatbotForm({ fields, onSubmit, brandColor, isSubmitted = false }: ChatbotFormProps) {
  const [formData, setFormData] = useState<Record<string, any>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [openPopovers, setOpenPopovers] = useState<Record<string, boolean>>({});
  const [countryCode, setCountryCode] = useState("+91");

  const handleChange = (name: string, value: any) => {
    setFormData((prev) => ({ ...prev, [name]: value }));
    if (errors[name]) {
      setErrors((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    }
  };

  const validateForm = () => {
    const newErrors: Record<string, string> = {};
    fields.forEach((field) => {
      if (field.required && !formData[field.name]) {
        newErrors[field.name] = `${field.label} is required`;
      }
      if (field.type === "email" && formData[field.name]) {
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(formData[field.name])) {
          newErrors[field.name] = "Invalid email address";
        }
      }
      if (field.type === "tel" && formData[field.name]) {
        const digits = formData[field.name].replace(/\D/g, "");
        if (digits.length < 7 || digits.length > 15) {
          newErrors[field.name] = "Enter a valid phone number (7-15 digits)";
        }
      }
      if (field.type === "otp" && formData[field.name]) {
        if (formData[field.name].length !== 4) {
          newErrors[field.name] = "Please enter the 4-digit OTP";
        }
      }
    });
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (isSubmitted) return;
    if (validateForm()) {
      const submitData = { ...formData };
      fields.forEach((field) => {
        if (field.type === "tel" && submitData[field.name]) {
          submitData[field.name] = `${countryCode} ${submitData[field.name]}`;
        }
      });
      onSubmit(submitData);
      setFormData({});
    } else {
      toast.error("Please fill in all required fields correctly");
    }
  };

  if (isSubmitted) {
    return (
      <div className="rounded-lg border bg-accent/30 p-3">
        <p className="text-xs font-medium text-muted-foreground">✅ Form submitted successfully</p>
      </div>
    );
  }

  const renderField = (field: FormField) => {
    switch (field.type) {
      case "select":
        return (
          <Select value={formData[field.name] || ""} onValueChange={(v) => handleChange(field.name, v)}>
            <SelectTrigger className="h-8 text-xs">
              <SelectValue placeholder={field.placeholder || "Select..."} />
            </SelectTrigger>
            <SelectContent>
              {field.options?.map((opt) => (
                <SelectItem key={opt} value={opt}>{opt}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        );

      case "date":
        return (
          <Popover
            open={openPopovers[field.name] || false}
            onOpenChange={(o) => setOpenPopovers((p) => ({ ...p, [field.name]: o }))}
          >
            <PopoverTrigger asChild>
              <button
                className={cn(
                  "flex h-8 w-full items-center gap-2 rounded-md border border-input bg-background px-2 text-xs",
                  !formData[field.name] && "text-muted-foreground"
                )}
              >
                <CalendarIcon className="h-3.5 w-3.5" />
                {formData[field.name] ? format(new Date(formData[field.name]), "PPP") : "Pick a date"}
              </button>
            </PopoverTrigger>
            <PopoverContent className="w-auto p-0" align="start">
              <Calendar
                mode="single"
                selected={formData[field.name] ? new Date(formData[field.name]) : undefined}
                onSelect={(date) => {
                  handleChange(field.name, date?.toISOString());
                  setOpenPopovers((p) => ({ ...p, [field.name]: false }));
                }}
                disabled={(date) => date < new Date()}
              />
            </PopoverContent>
          </Popover>
        );

      case "tel":
        return (
          <div className="flex gap-1">
            <Select value={countryCode} onValueChange={setCountryCode}>
              <SelectTrigger className="h-8 w-[90px] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {COUNTRY_CODES.map((c) => (
                  <SelectItem key={c.code} value={c.code}>
                    {c.flag} {c.code}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input
              type="tel"
              value={formData[field.name] || ""}
              onChange={(e) => handleChange(field.name, e.target.value)}
              placeholder={field.placeholder}
              className="h-8 flex-1 text-xs"
            />
          </div>
        );

      case "otp":
        return (
          <div className="flex justify-center gap-2">
            {[0, 1, 2, 3].map((i) => (
              <Input
                key={i}
                type="text"
                maxLength={1}
                value={(formData[field.name] || "")[i] || ""}
                onChange={(e) => {
                  const val = e.target.value.replace(/\D/g, "");
                  const current = formData[field.name] || "";
                  const arr = current.split("");
                  arr[i] = val;
                  handleChange(field.name, arr.join("").slice(0, 4));
                  if (val && e.target.nextElementSibling instanceof HTMLInputElement) {
                    e.target.nextElementSibling.focus();
                  }
                }}
                className="h-10 w-10 text-center text-sm font-bold"
              />
            ))}
          </div>
        );

      case "rating":
        return (
          <div className="flex justify-center gap-2">
            {FEEDBACK_RATINGS.map((r) => (
              <button
                key={r.value}
                type="button"
                onClick={() => handleChange(field.name, r.value)}
                className={cn(
                  "flex flex-col items-center rounded-lg p-2 transition-all",
                  formData[field.name] === r.value ? "scale-110 bg-accent ring-2" : "hover:bg-accent/50"
                )}
                style={formData[field.name] === r.value ? { outlineColor: brandColor, outlineWidth: 2, outlineStyle: "solid" as const } : {}}
              >
                <span className="text-2xl">{r.emoji}</span>
                <span className="mt-0.5 text-[10px]">{r.label}</span>
              </button>
            ))}
          </div>
        );

      case "tags":
        return (
          <div className="flex flex-wrap gap-1.5">
            {FEEDBACK_TAGS.map((tag) => {
              const selected = (formData[field.name] || []).includes(tag);
              return (
                <button
                  key={tag}
                  type="button"
                  onClick={() => {
                    const current = formData[field.name] || [];
                    handleChange(
                      field.name,
                      selected ? current.filter((t: string) => t !== tag) : [...current, tag]
                    );
                  }}
                  className="rounded-full border px-2.5 py-1 text-[11px] transition-colors"
                  style={
                    selected
                      ? { backgroundColor: brandColor, color: "#fff", borderColor: brandColor }
                      : { borderColor: `${brandColor}40`, color: brandColor }
                  }
                >
                  {tag}
                </button>
              );
            })}
          </div>
        );

      case "textarea":
        return (
          <Textarea
            value={formData[field.name] || ""}
            onChange={(e) => handleChange(field.name, e.target.value)}
            placeholder={field.placeholder}
            rows={2}
            className="text-xs"
          />
        );

      default:
        return (
          <Input
            type={field.type}
            value={formData[field.name] || ""}
            onChange={(e) => handleChange(field.name, e.target.value)}
            placeholder={field.placeholder}
            className="h-8 text-xs"
          />
        );
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-3 rounded-lg border bg-card/50 p-3">
      {fields.map((field) => (
        <div key={field.name} className="space-y-1">
          <Label className="text-[11px] font-medium">
            {field.label}
            {field.required && <span className="text-destructive"> *</span>}
          </Label>
          {renderField(field)}
          {errors[field.name] && (
            <p className="text-[10px] text-destructive">{errors[field.name]}</p>
          )}
        </div>
      ))}
      <Button
        type="submit"
        size="sm"
        className="w-full text-xs text-white"
        style={{ backgroundColor: brandColor }}
      >
        Submit
      </Button>
    </form>
  );
}
