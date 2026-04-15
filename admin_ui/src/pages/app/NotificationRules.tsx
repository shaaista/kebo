import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Plus, Trash2, Save, Mail, Phone, Pencil, Eye } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { bots, departments, triggerTypes, consolidationIntervals } from "@/data/operations";

interface NotificationRule {
  id: string;
  bot: string;
  departments: string[];
  trigger: string;
  assignTo: "auto" | "specific";
  recipients: string[];
  channelEmail: boolean;
  channelWhatsapp: boolean;
  consolidation: string;
  enabled: boolean;
}

const staffMembers = [
  { name: "Priya Sharma", role: "Manager", department: "Front Desk" },
  { name: "Rahul Verma", role: "Supervisor", department: "Housekeeping" },
  { name: "Anita Desai", role: "Executive", department: "Front Desk" },
  { name: "Vikram Patel", role: "Manager", department: "F&B" },
];

const defaultRules: NotificationRule[] = [
  { id: "nr1", bot: "Website Bot", departments: ["Front Desk"], trigger: "form-submission", assignTo: "specific", recipients: ["Priya Sharma", "Anita Desai"], channelEmail: true, channelWhatsapp: true, consolidation: "0", enabled: true },
  { id: "nr2", bot: "Booking Bot", departments: ["Front Desk"], trigger: "intent-detected", assignTo: "auto", recipients: [], channelEmail: true, channelWhatsapp: true, consolidation: "0", enabled: true },
  { id: "nr3", bot: "All Bots", departments: [], trigger: "escalation", assignTo: "specific", recipients: ["Priya Sharma", "Rahul Verma"], channelEmail: true, channelWhatsapp: false, consolidation: "0", enabled: true },
  { id: "nr4", bot: "All Bots", departments: [], trigger: "sla-breach", assignTo: "auto", recipients: [], channelEmail: true, channelWhatsapp: true, consolidation: "5", enabled: true },
  { id: "nr5", bot: "All Bots", departments: [], trigger: "sla-warning", assignTo: "auto", recipients: [], channelEmail: true, channelWhatsapp: true, consolidation: "5", enabled: true },
  { id: "nr6", bot: "All Bots", departments: [], trigger: "daily-summary", assignTo: "specific", recipients: ["Priya Sharma"], channelEmail: true, channelWhatsapp: false, consolidation: "0", enabled: false },
];

const triggerColorMap: Record<string, string> = {
  "form-submission": "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  "intent-detected": "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200",
  "ticket-created": "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  "sla-warning": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
  "sla-breach": "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
  "escalation": "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
  "daily-summary": "bg-slate-100 text-slate-800 dark:bg-slate-900 dark:text-slate-200",
};

const getTriggerLabel = (value: string) => triggerTypes.find((t) => t.value === value)?.label ?? value;

const emptyRule = (): NotificationRule => ({
  id: `nr-${Date.now()}`,
  bot: "All Bots",
  departments: [],
  trigger: "form-submission",
  assignTo: "specific",
  recipients: [],
  channelEmail: true,
  channelWhatsapp: false,
  consolidation: "0",
  enabled: true,
});

const NotificationRules = () => {
  const { toast } = useToast();
  const [rules, setRules] = useState<NotificationRule[]>(defaultRules);
  
  

  // Dialog state
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogMode, setDialogMode] = useState<"add" | "edit" | "view">("add");
  const [editingRule, setEditingRule] = useState<NotificationRule>(emptyRule());

  const isAllDepartments = (rule: NotificationRule) => !rule.departments || rule.departments.length === 0;
  const getDeptLabel = (rule: NotificationRule) => {
    if (!rule.departments || rule.departments.length === 0) return "All Departments";
    return rule.departments.join(", ");
  };

  const getStaffForDept = (depts: string[]) => {
    if (!depts || depts.length === 0) return staffMembers;
    return staffMembers.filter((s) => depts.includes(s.department));
  };

  // Duplicate validation
  const isDuplicate = (rule: NotificationRule, excludeId?: string) => {
    return rules.some((r) => {
      if (r.id === excludeId) return false;
      if (r.trigger !== rule.trigger) return false;
      if (r.bot !== rule.bot && r.bot !== "All Bots" && rule.bot !== "All Bots") return false;
      // Check department overlap
      const rAll = !r.departments || r.departments.length === 0;
      const newAll = !rule.departments || rule.departments.length === 0;
      if (rAll || newAll) return true;
      return r.departments.some((d) => rule.departments.includes(d));
    });
  };

  const openAdd = () => {
    setEditingRule(emptyRule());
    setDialogMode("add");
    setDialogOpen(true);
  };

  const openEdit = (rule: NotificationRule) => {
    setEditingRule({ ...rule });
    setDialogMode("edit");
    setDialogOpen(true);
  };

  const openView = (rule: NotificationRule) => {
    setEditingRule({ ...rule });
    setDialogMode("view");
    setDialogOpen(true);
  };

  const saveRule = () => {
    const excludeId = dialogMode === "edit" ? editingRule.id : undefined;
    if (isDuplicate(editingRule, excludeId)) {
      toast({
        title: "Duplicate rule detected",
        description: `A rule for "${getTriggerLabel(editingRule.trigger)}" on ${editingRule.bot} / ${getDeptLabel(editingRule)} already exists. Please edit the existing rule.`,
        variant: "destructive",
      });
      return;
    }
    if (dialogMode === "add") {
      setRules([...rules, editingRule]);
    } else {
      setRules(rules.map((r) => (r.id === editingRule.id ? editingRule : r)));
    }
    setDialogOpen(false);
    toast({ title: dialogMode === "add" ? "Rule added" : "Rule updated" });
  };

  const removeRule = (id: string) => setRules(rules.filter((r) => r.id !== id));
  const toggleEnabled = (id: string) => setRules(rules.map((r) => (r.id === id ? { ...r, enabled: !r.enabled } : r)));

  const toggleDepartment = (dept: string) => {
    const depts = editingRule.departments || [];
    setEditingRule({
      ...editingRule,
      departments: depts.includes(dept) ? depts.filter((d) => d !== dept) : [...depts, dept],
    });
  };

  const toggleRecipient = (name: string) => {
    const recipients = editingRule.recipients || [];
    setEditingRule({
      ...editingRule,
      recipients: recipients.includes(name) ? recipients.filter((r) => r !== name) : [...recipients, name],
    });
  };


  // Sort rules by trigger for grouping
  const sortedRules = [...rules].sort((a, b) => a.trigger.localeCompare(b.trigger));

  const isReadOnly = dialogMode === "view";

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Notification Rules</h1>
          <p className="text-muted-foreground">Configure who gets notified, when, and how — across bots and departments</p>
        </div>
        <Button onClick={() => toast({ title: "Notification rules saved" })}><Save className="mr-2 h-4 w-4" />Save Rules</Button>
      </div>



      {/* Rules List */}
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Notification Matrix</h2>
        <Button size="sm" onClick={openAdd}><Plus className="mr-2 h-4 w-4" />Add Rule</Button>
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[160px]">Trigger</TableHead>
                <TableHead>Bot</TableHead>
                <TableHead>Departments</TableHead>
                <TableHead>Assignment</TableHead>
                <TableHead>Channels</TableHead>
                <TableHead className="w-[80px]">Status</TableHead>
                <TableHead className="w-[120px] text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sortedRules.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="text-center py-8 text-muted-foreground">No notification rules defined. Add one to get started.</TableCell>
                </TableRow>
              ) : (
                sortedRules.map((rule) => (
                  <TableRow key={rule.id} className={!rule.enabled ? "opacity-50" : ""}>
                    <TableCell>
                      <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${triggerColorMap[rule.trigger] || "bg-muted text-muted-foreground"}`}>
                        {getTriggerLabel(rule.trigger)}
                      </span>
                    </TableCell>
                    <TableCell className="text-sm">{rule.bot}</TableCell>
                    <TableCell className="text-sm">{getDeptLabel(rule)}</TableCell>
                    <TableCell className="text-sm capitalize">{rule.assignTo === "auto" ? "Auto (round-robin)" : `${rule.recipients.length} staff`}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        {rule.channelEmail && <Mail className="h-3.5 w-3.5 text-muted-foreground" />}
                        {rule.channelWhatsapp && <Phone className="h-3.5 w-3.5 text-muted-foreground" />}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Switch checked={rule.enabled} onCheckedChange={() => toggleEnabled(rule.id)} />
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button variant="ghost" size="icon" onClick={() => openView(rule)}><Eye className="h-4 w-4" /></Button>
                        <Button variant="ghost" size="icon" onClick={() => openEdit(rule)}><Pencil className="h-4 w-4" /></Button>
                        <Button variant="ghost" size="icon" onClick={() => removeRule(rule.id)}><Trash2 className="h-4 w-4 text-destructive" /></Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {/* Add/Edit/View Dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              {dialogMode === "add" ? "Add Notification Rule" : dialogMode === "edit" ? "Edit Notification Rule" : "View Notification Rule"}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-5 py-2">
            {/* Trigger */}
            <div className="space-y-1.5">
              <Label className="text-xs text-muted-foreground">Trigger Type</Label>
              {isReadOnly ? (
                <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${triggerColorMap[editingRule.trigger] || "bg-muted"}`}>
                  {getTriggerLabel(editingRule.trigger)}
                </span>
              ) : (
                <Select value={editingRule.trigger} onValueChange={(v) => setEditingRule({ ...editingRule, trigger: v })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {triggerTypes.map((t) => <SelectItem key={t.value} value={t.value}>{t.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              )}
            </div>

            {/* Bot */}
            <div className="space-y-1.5">
              <Label className="text-xs text-muted-foreground">Bot</Label>
              {isReadOnly ? (
                <p className="text-sm">{editingRule.bot}</p>
              ) : (
                <Select value={editingRule.bot} onValueChange={(v) => setEditingRule({ ...editingRule, bot: v })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="All Bots">All Bots</SelectItem>
                    {bots.map((b) => <SelectItem key={b} value={b}>{b}</SelectItem>)}
                  </SelectContent>
                </Select>
              )}
            </div>

            {/* Departments */}
            <div className="space-y-1.5">
              <Label className="text-xs text-muted-foreground">Departments</Label>
              <div className="flex flex-wrap gap-1.5">
                {isReadOnly ? (
                  <p className="text-sm">{getDeptLabel(editingRule)}</p>
                ) : (
                  <>
                    <Badge
                      variant={isAllDepartments(editingRule) ? "default" : "outline"}
                      className="cursor-pointer select-none"
                      onClick={() => setEditingRule({ ...editingRule, departments: [] })}
                    >All</Badge>
                    {departments.map((d) => (
                      <Badge
                        key={d}
                        variant={(editingRule.departments || []).includes(d) ? "default" : "outline"}
                        className="cursor-pointer select-none"
                        onClick={() => {
                          if (isAllDepartments(editingRule)) {
                            setEditingRule({ ...editingRule, departments: [d] });
                          } else {
                            toggleDepartment(d);
                          }
                        }}
                      >{d}</Badge>
                    ))}
                  </>
                )}
              </div>
            </div>

            {/* Channels */}
            <div className="space-y-1.5">
              <Label className="text-xs text-muted-foreground">Channels</Label>
              <div className="flex items-center gap-4 pt-1">
                <label className="flex items-center gap-1.5 text-sm cursor-pointer">
                  <input type="checkbox" checked={editingRule.channelEmail} disabled={isReadOnly} onChange={(e) => setEditingRule({ ...editingRule, channelEmail: e.target.checked })} className="rounded" />
                  <Mail className="h-3.5 w-3.5" /> Email
                </label>
                <label className="flex items-center gap-1.5 text-sm cursor-pointer">
                  <input type="checkbox" checked={editingRule.channelWhatsapp} disabled={isReadOnly} onChange={(e) => setEditingRule({ ...editingRule, channelWhatsapp: e.target.checked })} className="rounded" />
                  <Phone className="h-3.5 w-3.5" /> WhatsApp
                </label>
              </div>
            </div>

            {/* Assignment + Consolidation */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label className="text-xs text-muted-foreground">Assignment Mode</Label>
                {isReadOnly ? (
                  <p className="text-sm">{editingRule.assignTo === "auto" ? "Auto-assign (round-robin)" : "Specific staff"}</p>
                ) : (
                  <Select value={editingRule.assignTo} onValueChange={(v: "auto" | "specific") => setEditingRule({ ...editingRule, assignTo: v })}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">Auto-assign (round-robin)</SelectItem>
                      <SelectItem value="specific">Specific staff</SelectItem>
                    </SelectContent>
                  </Select>
                )}
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs text-muted-foreground">Consolidation</Label>
                {isReadOnly ? (
                  <p className="text-sm">{consolidationIntervals.find((c) => c.value === editingRule.consolidation)?.label}</p>
                ) : (
                  <Select value={editingRule.consolidation} onValueChange={(v) => setEditingRule({ ...editingRule, consolidation: v })}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {consolidationIntervals.map((c) => <SelectItem key={c.value} value={c.value}>{c.label}</SelectItem>)}
                    </SelectContent>
                  </Select>
                )}
              </div>
            </div>

            {/* Recipients */}
            {editingRule.assignTo === "auto" ? (
              <div className="rounded-md border border-dashed p-3">
                <p className="text-sm text-muted-foreground">
                  Auto-assign will distribute tickets round-robin to active staff in <strong>{getDeptLabel(editingRule)}</strong>.
                </p>
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {getStaffForDept(editingRule.departments).map((s) => (
                    <Badge key={s.name} variant="outline" className="text-xs">
                      {s.name} <span className="ml-1 text-muted-foreground">({s.role})</span>
                    </Badge>
                  ))}
                </div>
              </div>
            ) : (
              <div className="space-y-1.5">
                <Label className="text-xs text-muted-foreground">Recipients</Label>
                <div className="flex flex-wrap gap-2">
                  {getStaffForDept(editingRule.departments).map((s) => (
                    <Badge
                      key={s.name}
                      variant={(editingRule.recipients || []).includes(s.name) ? "default" : "outline"}
                      className={isReadOnly ? "" : "cursor-pointer select-none"}
                      onClick={() => !isReadOnly && toggleRecipient(s.name)}
                    >
                      {s.name} <span className="ml-1 text-xs opacity-70">({s.role})</span>
                    </Badge>
                  ))}
                </div>
              </div>
            )}
          </div>

          {!isReadOnly && (
            <DialogFooter>
              <Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button>
              <Button onClick={saveRule}>{dialogMode === "add" ? "Add Rule" : "Save Changes"}</Button>
            </DialogFooter>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default NotificationRules;
