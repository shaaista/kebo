import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Loader2, RefreshCcw } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { adminGet } from "@/lib/adminApi";

interface EvaluationTabProps {
  propertyCode: string;
}

const EvaluationTab = ({ propertyCode }: EvaluationTabProps) => {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState<Record<string, unknown>>({});
  const [events, setEvents] = useState<Record<string, unknown>>({});

  const loadData = async () => {
    setLoading(true);
    try {
      const [summaryPayload, eventsPayload] = await Promise.all([
        adminGet<Record<string, unknown>>("/evaluation/summary", propertyCode),
        adminGet<Record<string, unknown>>("/evaluation/events?limit=100", propertyCode),
      ]);
      setSummary(summaryPayload || {});
      setEvents(eventsPayload || {});
    } catch (error) {
      toast({
        title: "Failed to load evaluation data",
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

  const score = Number(summary?.overall_score ?? 0);
  const passed = Number(summary?.passed_cases ?? 0);
  const failed = Number(summary?.failed_cases ?? 0);
  const total = Number(summary?.total_cases ?? 0);
  const lastRun = String(summary?.last_run_at || "-");

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">Evaluation metrics and recent evaluation events.</p>
        <Button variant="outline" size="sm" onClick={loadData} disabled={loading}>
          {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCcw className="mr-2 h-4 w-4" />}
          Refresh
        </Button>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">Overall Score</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{Number.isFinite(score) ? score.toFixed(2) : "-"}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">Passed Cases</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{passed}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">Failed Cases</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{failed}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">Total Cases</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{total}</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Run Status</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">Last run:</span>
            <Badge variant="outline">{lastRun}</Badge>
          </div>
          <pre className="max-h-72 overflow-auto rounded-md border bg-muted/40 p-3 text-xs">
            {JSON.stringify(summary, null, 2)}
          </pre>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Recent Events</CardTitle>
        </CardHeader>
        <CardContent>
          <pre className="max-h-96 overflow-auto rounded-md border bg-muted/40 p-3 text-xs">
            {JSON.stringify(events, null, 2)}
          </pre>
        </CardContent>
      </Card>
    </div>
  );
};

export default EvaluationTab;
