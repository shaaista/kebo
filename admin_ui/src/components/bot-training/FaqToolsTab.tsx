import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent } from "@/components/ui/card";
import { Plus, Trash2, Save, MessageSquare, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { adminGet, adminSend } from "@/lib/adminApi";

interface FaqEntry {
  id: string;
  question: string;
  answer: string;
}

interface FaqToolsTabProps {
  propertyCode: string;
}

const FaqToolsTab = ({ propertyCode }: FaqToolsTabProps) => {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [faqs, setFaqs] = useState<FaqEntry[]>([]);

  const faqIds = useMemo(() => new Set(faqs.map((f) => f.id).filter((id) => !id.startsWith("tmp-"))), [faqs]);

  const loadData = async () => {
    setLoading(true);
    try {
      const faqRows = await adminGet<Array<Record<string, unknown>>>("/config/faq-bank", propertyCode);

      setFaqs(
        (Array.isArray(faqRows) ? faqRows : []).map((row) => ({
          id: String(row.id || `tmp-${crypto.randomUUID()}`),
          question: String(row.question || ""),
          answer: String(row.answer || ""),
        })),
      );
    } catch (error) {
      toast({
        title: "Failed to load FAQ",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, [propertyCode]);

  const addFaq = () =>
    setFaqs((prev) => [...prev, { id: `tmp-${crypto.randomUUID()}`, question: "", answer: "" }]);

  const removeFaq = (id: string) => setFaqs((prev) => prev.filter((entry) => entry.id !== id));
  const updateFaq = (id: string, field: keyof FaqEntry, value: string) =>
    setFaqs((prev) => prev.map((entry) => (entry.id === id ? { ...entry, [field]: value } : entry)));

  const handleSave = async () => {
    setSaving(true);
    try {
      const serverFaqs = await adminGet<Array<Record<string, unknown>>>("/config/faq-bank", propertyCode);

      const serverFaqIds = new Set((Array.isArray(serverFaqs) ? serverFaqs : []).map((row) => String(row.id || "")));

      for (const faq of faqs) {
        if (!faq.question.trim() || !faq.answer.trim()) continue;
        const payload = {
          question: faq.question.trim(),
          answer: faq.answer.trim(),
          ...(faq.id.startsWith("tmp-") ? {} : { id: faq.id }),
        };
        if (faq.id.startsWith("tmp-")) {
          await adminSend("POST", "/config/faq-bank", payload, propertyCode);
        } else {
          await adminSend("PUT", `/config/faq-bank/${encodeURIComponent(faq.id)}`, payload, propertyCode);
        }
      }

      for (const id of serverFaqIds) {
        if (!faqIds.has(id)) {
          await adminSend("DELETE", `/config/faq-bank/${encodeURIComponent(id)}`, undefined, propertyCode);
        }
      }

      await loadData();
      toast({ title: "FAQ saved" });
    } catch (error) {
      toast({
        title: "Failed to save FAQ",
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
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading FAQ...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <MessageSquare className="h-4 w-4" /> Add FAQ question-answer pairs.
        </p>
        <Button size="sm" onClick={addFaq}>
          <Plus className="mr-2 h-4 w-4" /> Add FAQ
        </Button>
      </div>

      {faqs.map((faq) => (
        <Card key={faq.id}>
          <CardContent className="space-y-3 pt-4">
            <div className="flex items-start gap-3">
              <div className="flex-1 space-y-2">
                <Input
                  placeholder="Question"
                  value={faq.question}
                  onChange={(e) => updateFaq(faq.id, "question", e.target.value)}
                />
                <Textarea
                  placeholder="Answer"
                  value={faq.answer}
                  onChange={(e) => updateFaq(faq.id, "answer", e.target.value)}
                  rows={3}
                />
              </div>
              <Button variant="ghost" size="icon" onClick={() => removeFaq(faq.id)}>
                <Trash2 className="h-4 w-4 text-destructive" />
              </Button>
            </div>
          </CardContent>
        </Card>
      ))}

      <Button onClick={handleSave} disabled={saving}>
        {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
        Save FAQ
      </Button>
    </div>
  );
};

export default FaqToolsTab;
