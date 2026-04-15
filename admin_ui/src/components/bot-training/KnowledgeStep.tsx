import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

interface KnowledgeData {
  knowledgeSources: string;
  nluDoRules: string;
  nluDontRules: string;
  knowledgeNotes: string;
}

interface KnowledgeStepProps {
  data: KnowledgeData;
  onChange: (field: keyof KnowledgeData, value: string) => void;
  onSave: () => void;
}

const KnowledgeStep = ({ data, onChange, onSave }: KnowledgeStepProps) => (
  <Card>
    <CardHeader className="rounded-t-lg bg-primary px-6 py-4">
      <div className="flex items-center justify-between">
        <CardTitle className="text-lg text-primary-foreground">Step 4: Knowledge Base & NLU Rules</CardTitle>
        <Button size="sm" variant="secondary" onClick={onSave}>Save</Button>
      </div>
    </CardHeader>
    <CardContent className="space-y-4 p-6">
      <div className="space-y-1.5">
        <Label>Knowledge Sources</Label>
        <Textarea
          value={data.knowledgeSources}
          onChange={(e) => onChange("knowledgeSources", e.target.value)}
          placeholder={"One source per line:\nhttps://example.com/faq\n/data/menu.pdf\nhttps://docs.example.com"}
          rows={4}
        />
        <p className="text-xs text-muted-foreground">Enter file paths or URLs, one per line</p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>NLU Do Rules</Label>
          <Textarea
            value={data.nluDoRules}
            onChange={(e) => onChange("nluDoRules", e.target.value)}
            placeholder={"Always greet the user\nProvide pricing when asked\nOffer alternatives"}
            rows={5}
          />
        </div>
        <div className="space-y-1.5">
          <Label>NLU Don&apos;t Rules</Label>
          <Textarea
            value={data.nluDontRules}
            onChange={(e) => onChange("nluDontRules", e.target.value)}
            placeholder={"Don't share competitor info\nDon't make up prices\nDon't provide medical advice"}
            rows={5}
          />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label>Knowledge Notes</Label>
        <Textarea
          value={data.knowledgeNotes}
          onChange={(e) => onChange("knowledgeNotes", e.target.value)}
          placeholder="Additional context or notes for the knowledge base..."
          rows={3}
        />
      </div>
    </CardContent>
  </Card>
);

export default KnowledgeStep;
export type { KnowledgeData };
