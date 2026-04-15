import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription } from "@/components/ui/dialog";
import { Plus, Trash2, Save, ArrowRight, Settings2, AlertTriangle } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { bots, departments, roleHierarchy, assignmentMethods, fallbackBehaviors } from "@/data/operations";

interface PriorityLevel {
  id: string;
  name: string;
  color: string;
  bot: string;
  department: string;
  responseSlaMinutes: number;
  resolutionSlaMinutes: number;
  autoEscalateMinutes: number;
  escalationChain: string[];
  channel: string;
  enabled: boolean;
}

interface EscalationTrigger {
  id: string;
  type: string;
  condition: string;
  bot: string;
  department: string;
  priority: string;
  autoIncreasePriority: boolean;
  enabled: boolean;
}

interface EscalationTemplate {
  id: string;
  source: string;
  format: string;
  bot: string;
  department: string;
  message: string;
  includeTranscript: boolean;
  reminderEnabled: boolean;
  reminderMinutes: number;
  escalationType: string;
}

interface AssignmentConfig {
  id: string;
  bot: string;
  department: string;
  method: string;
  fallback: string;
  fallbackTimeoutMinutes: number;
  vipDirectToManager: boolean;
  enabled: boolean;
}

const staffMembers = [
  { name: "Priya Sharma", role: "Manager", department: "Front Desk" },
  { name: "Rahul Verma", role: "Supervisor", department: "Housekeeping" },
  { name: "Anita Desai", role: "Executive", department: "Front Desk" },
  { name: "Vikram Patel", role: "Manager", department: "F&B" },
];

const getChainForDept = (dept: string): string[] => {
  const deptStaff = dept === "All Departments"
    ? staffMembers
    : staffMembers.filter((s) => s.department === dept);
  return deptStaff
    .sort((a, b) => (roleHierarchy[a.role] || 0) - (roleHierarchy[b.role] || 0))
    .map((s) => s.name);
};

const triggerTypeOptions = [
  { value: "keyword", label: "Keyword" },
  { value: "sentiment", label: "Sentiment" },
  { value: "loop", label: "Loop Detection" },
  { value: "timeout", label: "Timeout" },
  { value: "low-rating", label: "Low Rating (≤3)" },
  { value: "manual-request", label: "Manual Request" },
  { value: "no-resolution", label: "No Resolution" },
];

const defaultPriorities: PriorityLevel[] = [
  { id: "p1", name: "Low", color: "secondary", bot: "All Bots", department: "All Departments", responseSlaMinutes: 240, resolutionSlaMinutes: 480, autoEscalateMinutes: 120, escalationChain: getChainForDept("All Departments"), channel: "Email", enabled: true },
  { id: "p2", name: "Medium", color: "default", bot: "All Bots", department: "All Departments", responseSlaMinutes: 60, resolutionSlaMinutes: 180, autoEscalateMinutes: 45, escalationChain: getChainForDept("All Departments"), channel: "Email", enabled: true },
  { id: "p3", name: "High", color: "default", bot: "All Bots", department: "Front Desk", responseSlaMinutes: 30, resolutionSlaMinutes: 60, autoEscalateMinutes: 15, escalationChain: getChainForDept("Front Desk"), channel: "Both", enabled: true },
  { id: "p4", name: "Critical", color: "destructive", bot: "All Bots", department: "All Departments", responseSlaMinutes: 10, resolutionSlaMinutes: 30, autoEscalateMinutes: 5, escalationChain: ["Priya Sharma"], channel: "WhatsApp", enabled: true },
];

const defaultTriggers: EscalationTrigger[] = [
  { id: "t1", type: "keyword", condition: "speak to manager, complaint, escalate", bot: "Website Bot", department: "Front Desk", priority: "High", autoIncreasePriority: false, enabled: true },
  { id: "t2", type: "sentiment", condition: "Negative sentiment detected 3+ times", bot: "All Bots", department: "All Departments", priority: "Medium", autoIncreasePriority: true, enabled: true },
  { id: "t3", type: "loop", condition: "Same question repeated 3 times", bot: "All Bots", department: "All Departments", priority: "Medium", autoIncreasePriority: false, enabled: true },
  { id: "t4", type: "timeout", condition: "No agent response within SLA", bot: "All Bots", department: "All Departments", priority: "Critical", autoIncreasePriority: true, enabled: true },
  { id: "t5", type: "low-rating", condition: "Guest rating ≤ 3 stars", bot: "All Bots", department: "All Departments", priority: "High", autoIncreasePriority: false, enabled: true },
  { id: "t6", type: "manual-request", condition: "Guest says 'talk to manager' or 'speak to someone'", bot: "All Bots", department: "All Departments", priority: "High", autoIncreasePriority: false, enabled: true },
  { id: "t7", type: "no-resolution", condition: "Bot fails to resolve after 5 attempts", bot: "All Bots", department: "All Departments", priority: "Medium", autoIncreasePriority: true, enabled: true },
];

const defaultTemplates: EscalationTemplate[] = [
  { id: "et1", source: "Chat Bot", format: "WhatsApp", bot: "Website Bot", department: "Front Desk", message: "🚨 Escalation from chat bot\n\nGuest: {{guest_name}}\nIssue: {{issue_summary}}\nPriority: {{priority}}", includeTranscript: true, reminderEnabled: true, reminderMinutes: 10, escalationType: "pre-breach" },
  { id: "et2", source: "About to Breach", format: "Email", bot: "All Bots", department: "All Departments", message: "⚠️ SLA About to Breach\n\nTicket: {{ticket_id}}\nGuest: {{guest_name}}\nPriority: {{priority}}\nTime remaining: {{time_remaining}}\n\nPlease respond before SLA expires.", includeTranscript: false, reminderEnabled: true, reminderMinutes: 15, escalationType: "pre-breach" },
  { id: "et3", source: "SLA Breach", format: "WhatsApp", bot: "All Bots", department: "All Departments", message: "⚠️ SLA BREACHED\n\nTicket: {{ticket_id}}\nOriginal Priority: {{priority}}\nTime elapsed: {{elapsed_time}}\n\nImmediate action required.", includeTranscript: true, reminderEnabled: true, reminderMinutes: 5, escalationType: "post-breach" },
];

const defaultAssignments: AssignmentConfig[] = [
  { id: "a1", bot: "All Bots", department: "Front Desk", method: "round-robin", fallback: "assign-manager", fallbackTimeoutMinutes: 10, vipDirectToManager: true, enabled: true },
  { id: "a2", bot: "All Bots", department: "Housekeeping", method: "load-based", fallback: "queue", fallbackTimeoutMinutes: 15, vipDirectToManager: false, enabled: true },
  { id: "a3", bot: "All Bots", department: "F&B", method: "skill-based", fallback: "bot-continue", fallbackTimeoutMinutes: 10, vipDirectToManager: true, enabled: true },
];

const EscalationMatrix = () => {
  const { toast } = useToast();
  const [priorities, setPriorities] = useState<PriorityLevel[]>(defaultPriorities);
  const [triggers, setTriggers] = useState<EscalationTrigger[]>(defaultTriggers);
  const [templates, setTemplates] = useState<EscalationTemplate[]>(defaultTemplates);
  const [assignments, setAssignments] = useState<AssignmentConfig[]>(defaultAssignments);
  const [filterBot, setFilterBot] = useState("All Bots");
  const [filterDept, setFilterDept] = useState("All Departments");
  const [addPriorityOpen, setAddPriorityOpen] = useState(false);
  const [newPriorityName, setNewPriorityName] = useState("");

  const updatePriority = (id: string, updates: Partial<PriorityLevel>) =>
    setPriorities(priorities.map((p) => (p.id === id ? { ...p, ...updates } : p)));

  const addPriority = () => {
    if (!newPriorityName.trim()) return;
    if (priorities.find((p) => p.name.toLowerCase() === newPriorityName.trim().toLowerCase())) {
      toast({ title: "Priority already exists", variant: "destructive" });
      return;
    }
    setPriorities([...priorities, {
      id: `p-${Date.now()}`, name: newPriorityName.trim(), color: "default",
      bot: filterBot, department: filterDept,
      responseSlaMinutes: 60, resolutionSlaMinutes: 120, autoEscalateMinutes: 30,
      escalationChain: getChainForDept(filterDept), channel: "Email", enabled: true,
    }]);
    setNewPriorityName("");
    setAddPriorityOpen(false);
    toast({ title: "Priority created" });
  };

  const removePriority = (id: string) => {
    setPriorities(priorities.filter((p) => p.id !== id));
    toast({ title: "Priority removed" });
  };

  const addTrigger = () => setTriggers([...triggers, { id: `t-${Date.now()}`, type: "keyword", condition: "", bot: filterBot, department: filterDept, priority: "Medium", autoIncreasePriority: false, enabled: true }]);
  const removeTrigger = (id: string) => setTriggers(triggers.filter((t) => t.id !== id));
  const updateTrigger = (id: string, updates: Partial<EscalationTrigger>) =>
    setTriggers(triggers.map((t) => (t.id === id ? { ...t, ...updates } : t)));

  const addTemplate = () => setTemplates([...templates, { id: `et-${Date.now()}`, source: "Chat Bot", format: "Email", bot: filterBot, department: filterDept, message: "", includeTranscript: false, reminderEnabled: false, reminderMinutes: 10, escalationType: "pre-breach" }]);
  const removeTemplate = (id: string) => setTemplates(templates.filter((t) => t.id !== id));
  const updateTemplate = (id: string, updates: Partial<EscalationTemplate>) =>
    setTemplates(templates.map((t) => (t.id === id ? { ...t, ...updates } : t)));

  const addAssignment = () => setAssignments([...assignments, { id: `a-${Date.now()}`, bot: filterBot, department: filterDept === "All Departments" ? "Front Desk" : filterDept, method: "round-robin", fallback: "queue", fallbackTimeoutMinutes: 10, vipDirectToManager: false, enabled: true }]);
  const removeAssignment = (id: string) => setAssignments(assignments.filter((a) => a.id !== id));
  const updateAssignment = (id: string, updates: Partial<AssignmentConfig>) =>
    setAssignments(assignments.map((a) => (a.id === id ? { ...a, ...updates } : a)));

  const toggleChainMember = (priorityId: string, name: string) => {
    const p = priorities.find((pr) => pr.id === priorityId);
    if (!p) return;
    const chain = p.escalationChain.includes(name)
      ? p.escalationChain.filter((s) => s !== name)
      : [...p.escalationChain, name];
    updatePriority(priorityId, { escalationChain: chain });
  };

  const filterItem = (item: { bot: string; department: string }) => {
    const botMatch = filterBot === "All Bots" || item.bot === "All Bots" || item.bot === filterBot;
    const deptMatch = filterDept === "All Departments" || item.department === "All Departments" || item.department === filterDept;
    return botMatch && deptMatch;
  };

  const filteredPriorities = priorities.filter(filterItem);
  const filteredTriggers = triggers.filter(filterItem);
  const filteredTemplates = templates.filter(filterItem);
  const filteredAssignments = assignments.filter(filterItem);

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Escalation Matrix</h1>
          <p className="text-muted-foreground">Configure priorities, triggers, SLA, assignment rules, and escalation workflows</p>
        </div>
        <Button onClick={() => toast({ title: "Escalation matrix saved" })}><Save className="mr-2 h-4 w-4" />Save All</Button>
      </div>

      {/* Scope Filters */}
      <div className="flex items-center gap-3 flex-wrap">
        <Label className="text-sm">Bot:</Label>
        <Select value={filterBot} onValueChange={setFilterBot}>
          <SelectTrigger className="w-[160px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="All Bots">All Bots</SelectItem>
            {bots.map((b) => <SelectItem key={b} value={b}>{b}</SelectItem>)}
          </SelectContent>
        </Select>
        <Label className="text-sm">Department:</Label>
        <Select value={filterDept} onValueChange={setFilterDept}>
          <SelectTrigger className="w-[160px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="All Departments">All Departments</SelectItem>
            {departments.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>

      <Tabs defaultValue="priorities">
        <TabsList>
          <TabsTrigger value="priorities">Priority Levels</TabsTrigger>
          <TabsTrigger value="triggers">Triggers</TabsTrigger>
          <TabsTrigger value="assignment">Assignment Rules</TabsTrigger>
          <TabsTrigger value="templates">Notification Templates</TabsTrigger>
        </TabsList>

        {/* PRIORITIES */}
        <TabsContent value="priorities" className="mt-4 space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">Define response & resolution SLAs per priority level.</p>
            <Button size="sm" onClick={() => setAddPriorityOpen(true)}><Plus className="mr-2 h-4 w-4" />Add Priority</Button>
          </div>
          {filteredPriorities.map((p) => (
            <Card key={p.id}>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Badge variant={p.color as "default" | "secondary" | "destructive"}>{p.name}</Badge>
                    <CardTitle className="text-base">{p.name} Priority</CardTitle>
                    <span className="text-xs text-muted-foreground">({p.bot} · {p.department})</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Switch checked={p.enabled} onCheckedChange={(v) => updatePriority(p.id, { enabled: v })} />
                    {!["p1", "p2", "p3", "p4"].includes(p.id) && (
                      <Button variant="ghost" size="icon" onClick={() => removePriority(p.id)}><Trash2 className="h-4 w-4 text-destructive" /></Button>
                    )}
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Bot Scope</Label>
                    <Select value={p.bot} onValueChange={(v) => updatePriority(p.id, { bot: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="All Bots">All Bots</SelectItem>
                        {bots.map((b) => <SelectItem key={b} value={b}>{b}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Department Scope</Label>
                    <Select value={p.department} onValueChange={(v) => {
                      updatePriority(p.id, { department: v, escalationChain: getChainForDept(v) });
                    }}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="All Departments">All Departments</SelectItem>
                        {departments.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Response SLA (min)</Label>
                    <Input type="number" value={p.responseSlaMinutes} onChange={(e) => updatePriority(p.id, { responseSlaMinutes: Number(e.target.value) })} />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Resolution SLA (min)</Label>
                    <Input type="number" value={p.resolutionSlaMinutes} onChange={(e) => updatePriority(p.id, { resolutionSlaMinutes: Number(e.target.value) })} />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Auto-escalate (min)</Label>
                    <Input type="number" value={p.autoEscalateMinutes} onChange={(e) => updatePriority(p.id, { autoEscalateMinutes: Number(e.target.value) })} />
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Notification Channel</Label>
                    <Select value={p.channel} onValueChange={(v) => updatePriority(p.id, { channel: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="Email">Email</SelectItem>
                        <SelectItem value="WhatsApp">WhatsApp</SelectItem>
                        <SelectItem value="Both">Both</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                {/* Escalation Chain */}
                <div className="space-y-1.5">
                  <Label className="text-xs text-muted-foreground">Escalation Chain (ordered by role hierarchy)</Label>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {p.escalationChain.map((name, i) => {
                      const member = staffMembers.find((s) => s.name === name);
                      return (
                        <span key={name} className="flex items-center gap-1">
                          <Badge
                            variant="default"
                            className="cursor-pointer select-none"
                            onClick={() => toggleChainMember(p.id, name)}
                          >
                            {name} <span className="ml-1 text-xs opacity-70">({member?.role})</span>
                          </Badge>
                          {i < p.escalationChain.length - 1 && <ArrowRight className="h-3 w-3 text-muted-foreground" />}
                        </span>
                      );
                    })}
                  </div>
                  {(() => {
                    const available = (p.department === "All Departments" ? staffMembers : staffMembers.filter((s) => s.department === p.department))
                      .filter((s) => !p.escalationChain.includes(s.name));
                    if (available.length === 0) return null;
                    return (
                      <div className="flex flex-wrap gap-1.5 mt-1">
                        {available.map((s) => (
                          <Badge
                            key={s.name}
                            variant="outline"
                            className="cursor-pointer select-none opacity-60"
                            onClick={() => toggleChainMember(p.id, s.name)}
                          >
                            + {s.name}
                          </Badge>
                        ))}
                      </div>
                    );
                  })()}
                </div>
              </CardContent>
            </Card>
          ))}
          {filteredPriorities.length === 0 && (
            <Card><CardContent className="py-8 text-center text-muted-foreground">No priorities match this bot/department scope.</CardContent></Card>
          )}
        </TabsContent>

        {/* TRIGGERS */}
        <TabsContent value="triggers" className="mt-4 space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">Define conditions that trigger escalation to human agents.</p>
            <Button size="sm" onClick={addTrigger}><Plus className="mr-2 h-4 w-4" />Add Trigger</Button>
          </div>

          <Card>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Type</TableHead>
                    <TableHead>Condition</TableHead>
                    <TableHead>Bot</TableHead>
                    <TableHead>Dept</TableHead>
                    <TableHead>Priority</TableHead>
                    <TableHead>Auto ↑</TableHead>
                    <TableHead>Active</TableHead>
                    <TableHead className="w-[60px]" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredTriggers.map((t) => (
                    <TableRow key={t.id}>
                      <TableCell>
                        <Select value={t.type} onValueChange={(v) => updateTrigger(t.id, { type: v })}>
                          <SelectTrigger className="w-[140px]"><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {triggerTypeOptions.map((opt) => (
                              <SelectItem key={opt.value} value={opt.value}>{opt.label}</SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell>
                        <Input value={t.condition} onChange={(e) => updateTrigger(t.id, { condition: e.target.value })} className="min-w-[160px]" />
                      </TableCell>
                      <TableCell>
                        <Select value={t.bot} onValueChange={(v) => updateTrigger(t.id, { bot: v })}>
                          <SelectTrigger className="w-[130px]"><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="All Bots">All Bots</SelectItem>
                            {bots.map((b) => <SelectItem key={b} value={b}>{b}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell>
                        <Select value={t.department} onValueChange={(v) => updateTrigger(t.id, { department: v })}>
                          <SelectTrigger className="w-[130px]"><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="All Departments">All Depts</SelectItem>
                            {departments.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell>
                        <Select value={t.priority} onValueChange={(v) => updateTrigger(t.id, { priority: v })}>
                          <SelectTrigger className="w-[110px]"><SelectValue /></SelectTrigger>
                          <SelectContent>
                            {priorities.map((p) => <SelectItem key={p.id} value={p.name}>{p.name}</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell>
                        <Switch checked={t.autoIncreasePriority} onCheckedChange={(v) => updateTrigger(t.id, { autoIncreasePriority: v })} />
                      </TableCell>
                      <TableCell><Switch checked={t.enabled} onCheckedChange={(v) => updateTrigger(t.id, { enabled: v })} /></TableCell>
                      <TableCell><Button variant="ghost" size="icon" onClick={() => removeTrigger(t.id)}><Trash2 className="h-4 w-4 text-destructive" /></Button></TableCell>
                    </TableRow>
                  ))}
                  {filteredTriggers.length === 0 && (
                    <TableRow><TableCell colSpan={8} className="py-8 text-center text-muted-foreground">No triggers match this scope.</TableCell></TableRow>
                  )}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </TabsContent>

        {/* ASSIGNMENT RULES */}
        <TabsContent value="assignment" className="mt-4 space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">Configure how tickets are assigned to staff and what happens when all agents are busy.</p>
            <Button size="sm" onClick={addAssignment}><Plus className="mr-2 h-4 w-4" />Add Rule</Button>
          </div>

          {filteredAssignments.map((a) => (
            <Card key={a.id}>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Settings2 className="h-4 w-4 text-primary" />
                    <CardTitle className="text-base">{a.department}</CardTitle>
                    <span className="text-xs text-muted-foreground">({a.bot})</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Switch checked={a.enabled} onCheckedChange={(v) => updateAssignment(a.id, { enabled: v })} />
                    <Button variant="ghost" size="icon" onClick={() => removeAssignment(a.id)}><Trash2 className="h-4 w-4 text-destructive" /></Button>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Bot Scope</Label>
                    <Select value={a.bot} onValueChange={(v) => updateAssignment(a.id, { bot: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="All Bots">All Bots</SelectItem>
                        {bots.map((b) => <SelectItem key={b} value={b}>{b}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Department</Label>
                    <Select value={a.department} onValueChange={(v) => updateAssignment(a.id, { department: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {departments.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs text-muted-foreground">Assignment Method</Label>
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                    {assignmentMethods.map((m) => (
                      <div
                        key={m.value}
                        className={`rounded-lg border p-3 cursor-pointer transition-colors ${a.method === m.value ? "border-primary bg-primary/5" : "hover:border-muted-foreground/30"}`}
                        onClick={() => updateAssignment(a.id, { method: m.value })}
                      >
                        <p className="text-sm font-medium">{m.label}</p>
                        <p className="text-xs text-muted-foreground mt-0.5">{m.description}</p>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="rounded-lg border p-3 bg-muted/30 space-y-3">
                  <div className="flex items-center gap-2">
                    <AlertTriangle className="h-4 w-4 text-amber-500" />
                    <Label className="text-sm font-medium">When All Agents Are Busy</Label>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div className="space-y-1.5">
                      <Label className="text-xs text-muted-foreground">Fallback Behavior</Label>
                      <Select value={a.fallback} onValueChange={(v) => updateAssignment(a.id, { fallback: v })}>
                        <SelectTrigger><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {fallbackBehaviors.map((f) => (
                            <SelectItem key={f.value} value={f.value}>{f.label}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        {fallbackBehaviors.find((f) => f.value === a.fallback)?.description}
                      </p>
                    </div>
                    {a.fallback === "auto-escalate" && (
                      <div className="space-y-1.5">
                        <Label className="text-xs text-muted-foreground">Escalate after (minutes)</Label>
                        <Input type="number" value={a.fallbackTimeoutMinutes} onChange={(e) => updateAssignment(a.id, { fallbackTimeoutMinutes: Number(e.target.value) })} />
                      </div>
                    )}
                  </div>
                  <label className="flex items-center gap-2 text-sm cursor-pointer">
                    <Switch checked={a.vipDirectToManager} onCheckedChange={(v) => updateAssignment(a.id, { vipDirectToManager: v })} />
                    VIP guests bypass queue — route directly to manager
                  </label>
                </div>
              </CardContent>
            </Card>
          ))}
          {filteredAssignments.length === 0 && (
            <Card><CardContent className="py-8 text-center text-muted-foreground">No assignment rules match this scope. Add one to configure ticket routing.</CardContent></Card>
          )}
        </TabsContent>

        {/* TEMPLATES */}
        <TabsContent value="templates" className="mt-4 space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">Define the format and content of notification templates sent per source.</p>
            <Button size="sm" onClick={addTemplate}><Plus className="mr-2 h-4 w-4" />Add Template</Button>
          </div>

          {filteredTemplates.map((t) => (
            <Card key={t.id}>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                   <div className="flex items-center gap-2">
                    <CardTitle className="text-base">{t.source} — {t.format}</CardTitle>
                    <Badge variant={t.escalationType === "post-breach" ? "destructive" : "secondary"}>{t.escalationType}</Badge>
                    <span className="text-xs text-muted-foreground">({t.bot} · {t.department})</span>
                  </div>
                  <Button variant="ghost" size="icon" onClick={() => removeTemplate(t.id)}><Trash2 className="h-4 w-4 text-destructive" /></Button>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Source</Label>
                    <Select value={t.source} onValueChange={(v) => updateTemplate(t.id, { source: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="Chat Bot">Chat Bot</SelectItem>
                        <SelectItem value="About to Breach">About to Breach</SelectItem>
                        <SelectItem value="SLA Breach">SLA Breach</SelectItem>
                        <SelectItem value="Manual">Manual</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Format</Label>
                    <Select value={t.format} onValueChange={(v) => updateTemplate(t.id, { format: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="Email">Email</SelectItem>
                        <SelectItem value="WhatsApp">WhatsApp</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Bot</Label>
                    <Select value={t.bot} onValueChange={(v) => updateTemplate(t.id, { bot: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="All Bots">All Bots</SelectItem>
                        {bots.map((b) => <SelectItem key={b} value={b}>{b}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Escalation Type</Label>
                    <Select value={t.escalationType} onValueChange={(v) => updateTemplate(t.id, { escalationType: v })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="pre-breach">Pre-Breach</SelectItem>
                        <SelectItem value="post-breach">Post-Breach</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
                <div className="space-y-1.5">
                  <Label className="text-xs text-muted-foreground">Message Template</Label>
                  <Textarea value={t.message} onChange={(e) => updateTemplate(t.id, { message: e.target.value })} rows={4} className="font-mono text-sm" />
                </div>
                <div className="flex flex-wrap items-center gap-6">
                  <label className="flex items-center gap-2 text-sm">
                    <Switch checked={t.includeTranscript} onCheckedChange={(v) => updateTemplate(t.id, { includeTranscript: v })} />
                    Include chat transcript
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <Switch checked={t.reminderEnabled} onCheckedChange={(v) => updateTemplate(t.id, { reminderEnabled: v })} />
                    Send reminder
                  </label>
                  {t.reminderEnabled && (
                    <div className="flex items-center gap-2">
                      <Label className="text-xs text-muted-foreground">after</Label>
                      <Input type="number" value={t.reminderMinutes} onChange={(e) => updateTemplate(t.id, { reminderMinutes: Number(e.target.value) })} className="w-20" />
                      <span className="text-xs text-muted-foreground">min</span>
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
          {filteredTemplates.length === 0 && (
            <Card><CardContent className="py-8 text-center text-muted-foreground">No templates match this scope.</CardContent></Card>
          )}
        </TabsContent>
      </Tabs>

      {/* Add Priority Dialog */}
      <Dialog open={addPriorityOpen} onOpenChange={setAddPriorityOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Add Custom Priority</DialogTitle>
            <DialogDescription>Create a new priority level with default SLA values.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label>Priority Name</Label>
              <Input value={newPriorityName} onChange={(e) => setNewPriorityName(e.target.value)} placeholder="e.g., Urgent, VIP" />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddPriorityOpen(false)}>Cancel</Button>
            <Button onClick={addPriority}>Create</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default EscalationMatrix;
