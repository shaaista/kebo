import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Plus, Pencil, Trash2, Clock, ChevronDown, ChevronRight, Mail, Phone, Shield, Save } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { defaultDepartments, DepartmentConfig, weekDays, fallbackBehaviors, defaultStaff, StaffMember, roleHierarchy, availabilityStatuses } from "@/data/operations";

const staffMembers = [
  { name: "Priya Sharma", role: "Manager", department: "Front Desk" },
  { name: "Rahul Verma", role: "Supervisor", department: "Housekeeping" },
  { name: "Anita Desai", role: "Executive", department: "Front Desk" },
  { name: "Vikram Patel", role: "Manager", department: "F&B" },
];

const emptyDept: Omit<DepartmentConfig, "id"> = {
  name: "", manager: "", escalationManager: "",
  workingHoursFrom: "09:00", workingHoursTo: "18:00",
  workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri"],
  is24x7: false, afterHoursBehavior: "queue",
  afterHoursMessage: "", enabled: true,
};

const getStatusColor = (status: string) => {
  const s = availabilityStatuses.find((a) => a.value === status);
  return s?.color || "bg-muted-foreground";
};

const Departments = () => {
  const { toast } = useToast();
  const [depts, setDepts] = useState<DepartmentConfig[]>(defaultDepartments);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<Omit<DepartmentConfig, "id">>(emptyDept);
  const [expandedDepts, setExpandedDepts] = useState<Set<string>>(new Set());

  const openAdd = () => { setEditingId(null); setForm(emptyDept); setDialogOpen(true); };
  const openEdit = (d: DepartmentConfig) => { setEditingId(d.id); setForm({ ...d }); setDialogOpen(true); };

  const toggleExpand = (id: string) => {
    setExpandedDepts((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const getStaffForDept = (deptName: string): StaffMember[] => {
    return defaultStaff
      .filter((s) => s.departments.includes(deptName))
      .sort((a, b) => (roleHierarchy[b.role] || 0) - (roleHierarchy[a.role] || 0));
  };

  const save = () => {
    if (!form.name.trim()) {
      toast({ title: "Department name is required", variant: "destructive" });
      return;
    }
    const duplicate = depts.find((d) => d.name.toLowerCase() === form.name.trim().toLowerCase() && d.id !== editingId);
    if (duplicate) {
      toast({ title: "Department already exists", variant: "destructive" });
      return;
    }
    if (editingId) {
      setDepts(depts.map((d) => (d.id === editingId ? { ...d, ...form } : d)));
    } else {
      setDepts([...depts, { id: `d-${Date.now()}`, ...form }]);
    }
    setDialogOpen(false);
    toast({ title: editingId ? "Department updated" : "Department created" });
  };

  const remove = (id: string) => {
    setDepts(depts.filter((d) => d.id !== id));
    toast({ title: "Department removed" });
  };

  const toggleDay = (day: string) => {
    const updated = form.workingDays.includes(day)
      ? form.workingDays.filter((d) => d !== day)
      : [...form.workingDays, day];
    setForm({ ...form, workingDays: updated });
  };

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Department Configuration</h1>
          <p className="text-muted-foreground">Manage departments, working hours, managers, and after-hours behavior</p>
        </div>
        <Button onClick={openAdd}><Plus className="mr-2 h-4 w-4" />Add Department</Button>
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[40px]"></TableHead>
                <TableHead>Department</TableHead>
                <TableHead>Manager</TableHead>
                <TableHead>Esc. Manager</TableHead>
                <TableHead>Staff</TableHead>
                <TableHead>Working Hours</TableHead>
                <TableHead>After Hours</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="w-[100px]">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {depts.map((d) => {
                const deptStaff = getStaffForDept(d.name);
                const isExpanded = expandedDepts.has(d.id);

                return (
                  <Collapsible key={d.id} open={isExpanded} onOpenChange={() => toggleExpand(d.id)} asChild>
                    <>
                      <TableRow className={!d.enabled ? "opacity-50" : ""}>
                        <TableCell className="px-2">
                          <CollapsibleTrigger asChild>
                            <Button variant="ghost" size="icon" className="h-6 w-6" disabled={deptStaff.length === 0}>
                              {isExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                            </Button>
                          </CollapsibleTrigger>
                        </TableCell>
                        <TableCell className="font-medium">{d.name}</TableCell>
                        <TableCell className="text-sm">{d.manager || <span className="text-muted-foreground">Unassigned</span>}</TableCell>
                        <TableCell className="text-sm">{d.escalationManager || <span className="text-muted-foreground">Unassigned</span>}</TableCell>
                        <TableCell>
                          <Badge variant="secondary" className="text-xs">
                            {deptStaff.length} {deptStaff.length === 1 ? "member" : "members"}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1 text-sm text-muted-foreground">
                            <Clock className="h-3.5 w-3.5" />
                            {d.is24x7 ? "24/7" : `${d.workingHoursFrom} – ${d.workingHoursTo}`}
                          </div>
                          <div className="flex gap-0.5 mt-1">
                            {weekDays.map((day) => (
                              <span key={day} className={`text-[10px] px-1 rounded ${d.workingDays.includes(day) ? "bg-primary/10 text-primary font-medium" : "text-muted-foreground/40"}`}>
                                {day.charAt(0)}
                              </span>
                            ))}
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline" className="text-xs">
                            {fallbackBehaviors.find((f) => f.value === d.afterHoursBehavior)?.label || d.afterHoursBehavior}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <Switch checked={d.enabled} onCheckedChange={(v) => setDepts(depts.map((dept) => dept.id === d.id ? { ...dept, enabled: v } : dept))} />
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1">
                            <Button variant="ghost" size="icon" onClick={() => openEdit(d)}><Pencil className="h-4 w-4" /></Button>
                            <Button variant="ghost" size="icon" onClick={() => remove(d.id)}><Trash2 className="h-4 w-4 text-destructive" /></Button>
                          </div>
                        </TableCell>
                      </TableRow>
                      <CollapsibleContent asChild>
                        <tr>
                          <td colSpan={9} className="p-0">
                            <div className="bg-muted/30 border-t px-6 py-3">
                              <p className="text-xs font-medium text-muted-foreground mb-2">Staff Roster — sorted by role hierarchy</p>
                              <div className="rounded-md border bg-background">
                                <Table>
                                  <TableHeader>
                                    <TableRow className="text-xs">
                                      <TableHead className="h-8">Name</TableHead>
                                      <TableHead className="h-8">Role</TableHead>
                                      <TableHead className="h-8">Status</TableHead>
                                      <TableHead className="h-8">Channels</TableHead>
                                      <TableHead className="h-8">Tags</TableHead>
                                    </TableRow>
                                  </TableHeader>
                                  <TableBody>
                                    {deptStaff.map((s) => (
                                      <TableRow key={s.id} className="text-sm">
                                        <TableCell className="py-2 font-medium">{s.name}</TableCell>
                                        <TableCell className="py-2">{s.role}</TableCell>
                                        <TableCell className="py-2">
                                          <div className="flex items-center gap-1.5">
                                            <span className={`h-2 w-2 rounded-full ${getStatusColor(s.status)}`} />
                                            <span className="text-xs text-muted-foreground capitalize">{s.status}</span>
                                          </div>
                                        </TableCell>
                                        <TableCell className="py-2">
                                          <div className="flex items-center gap-2">
                                            {s.emailEnabled && <Mail className="h-3.5 w-3.5 text-muted-foreground" />}
                                            {s.whatsappEnabled && <Phone className="h-3.5 w-3.5 text-muted-foreground" />}
                                          </div>
                                        </TableCell>
                                        <TableCell className="py-2">
                                          <div className="flex items-center gap-1">
                                            {d.manager === s.name && <Badge variant="default" className="text-[10px] px-1.5 py-0">Manager</Badge>}
                                            {s.escalationLevels.length > 0 && <Badge variant="outline" className="text-[10px] px-1.5 py-0 gap-0.5"><Shield className="h-2.5 w-2.5" />{s.escalationLevels.join(", ")}</Badge>}
                                          </div>
                                        </TableCell>
                                      </TableRow>
                                    ))}
                                    {deptStaff.length === 0 && (
                                      <TableRow>
                                        <TableCell colSpan={5} className="text-center text-muted-foreground py-4 text-sm">No staff assigned to this department</TableCell>
                                      </TableRow>
                                    )}
                                  </TableBody>
                                </Table>
                              </div>
                            </div>
                          </td>
                        </tr>
                      </CollapsibleContent>
                    </>
                  </Collapsible>
                );
              })}
              {depts.length === 0 && (
                <TableRow><TableCell colSpan={9} className="text-center text-muted-foreground py-8">No departments configured</TableCell></TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editingId ? "Edit Department" : "Add Department"}</DialogTitle>
            <DialogDescription>Configure department details, working hours, and after-hours behavior.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label>Department Name *</Label>
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g., Spa & Wellness" />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Manager</Label>
                <Select value={form.manager || "__none"} onValueChange={(v) => setForm({ ...form, manager: v === "__none" ? "" : v })}>
                  <SelectTrigger><SelectValue placeholder="Select manager" /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none">Unassigned</SelectItem>
                    {staffMembers.filter((s) => s.role === "Manager" || s.role === "Supervisor").map((s) => (
                      <SelectItem key={s.name} value={s.name}>{s.name} ({s.role})</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Escalation Manager</Label>
                <Select value={form.escalationManager || "__none"} onValueChange={(v) => setForm({ ...form, escalationManager: v === "__none" ? "" : v })}>
                  <SelectTrigger><SelectValue placeholder="Select escalation mgr" /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none">Unassigned</SelectItem>
                    {staffMembers.filter((s) => s.role === "Manager" || s.role === "Supervisor").map((s) => (
                      <SelectItem key={s.name} value={s.name}>{s.name} ({s.role})</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            {/* Working Hours */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>Working Hours</Label>
                <label className="flex items-center gap-2 text-sm cursor-pointer">
                  <Switch checked={form.is24x7} onCheckedChange={(v) => setForm({ ...form, is24x7: v })} />
                  24/7 Operation
                </label>
              </div>
              {!form.is24x7 && (
                <>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1.5">
                      <Label className="text-xs text-muted-foreground">From</Label>
                      <Input type="time" value={form.workingHoursFrom} onChange={(e) => setForm({ ...form, workingHoursFrom: e.target.value })} />
                    </div>
                    <div className="space-y-1.5">
                      <Label className="text-xs text-muted-foreground">To</Label>
                      <Input type="time" value={form.workingHoursTo} onChange={(e) => setForm({ ...form, workingHoursTo: e.target.value })} />
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">Working Days</Label>
                    <div className="flex gap-1.5">
                      {weekDays.map((day) => (
                        <Badge
                          key={day}
                          variant={form.workingDays.includes(day) ? "default" : "outline"}
                          className="cursor-pointer select-none px-2.5 py-1"
                          onClick={() => toggleDay(day)}
                        >
                          {day}
                        </Badge>
                      ))}
                    </div>
                  </div>
                </>
              )}
            </div>

            {/* After-Hours Behavior */}
            {!form.is24x7 && (
              <div className="space-y-2 rounded-lg border p-3 bg-muted/30">
                <Label className="text-sm font-medium">After-Hours Behavior</Label>
                <Select value={form.afterHoursBehavior} onValueChange={(v) => setForm({ ...form, afterHoursBehavior: v })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {fallbackBehaviors.map((f) => (
                      <SelectItem key={f.value} value={f.value}>
                        {f.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {fallbackBehaviors.find((f) => f.value === form.afterHoursBehavior)?.description}
                </p>
                <div className="space-y-1.5">
                  <Label className="text-xs text-muted-foreground">Auto-Reply Message</Label>
                  <Textarea
                    value={form.afterHoursMessage}
                    onChange={(e) => setForm({ ...form, afterHoursMessage: e.target.value })}
                    rows={2}
                    placeholder="Message shown to guests outside working hours..."
                  />
                </div>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button>
            <Button onClick={save}>{editingId ? "Update" : "Create"}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default Departments;