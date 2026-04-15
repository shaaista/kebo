import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Plus, Pencil, Trash2, Phone, Mail, Shield } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { bots, departments, roles, availabilityStatuses, AvailabilityStatus, defaultStaff, StaffMember, weekDays, escalationLevels, EscalationLevel } from "@/data/operations";

const emptyStaff: Omit<StaffMember, "id"> = {
  name: "", role: "Agent", departments: [], email: "", whatsapp: "",
  emailEnabled: true, whatsappEnabled: false,
  availabilityFrom: "09:00", availabilityTo: "18:00",
  workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri"],
  assignedBots: [], escalationLevels: [], status: "online",
};

const StaffManagement = () => {
  const { toast } = useToast();
  const [staff, setStaff] = useState<StaffMember[]>(defaultStaff);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<Omit<StaffMember, "id">>(emptyStaff);
  const [filterDept, setFilterDept] = useState<string>("all");
  const [filterBot, setFilterBot] = useState<string>("all");

  const openAdd = () => { setEditingId(null); setForm(emptyStaff); setDialogOpen(true); };
  const openEdit = (s: StaffMember) => { setEditingId(s.id); setForm({ ...s }); setDialogOpen(true); };

  const save = () => {
    if (!form.name || !form.email) { toast({ title: "Name and email are required", variant: "destructive" }); return; }
    if (form.departments.length === 0) { toast({ title: "Assign at least one department", variant: "destructive" }); return; }
    if (form.assignedBots.length === 0) { toast({ title: "Assign at least one bot", variant: "destructive" }); return; }
    if (editingId) {
      setStaff(staff.map((s) => (s.id === editingId ? { ...s, ...form } : s)));
    } else {
      setStaff([...staff, { id: `s-${Date.now()}`, ...form }]);
    }
    setDialogOpen(false);
    toast({ title: editingId ? "Staff updated" : "Staff added" });
  };

  const remove = (id: string) => {
    setStaff(staff.filter((s) => s.id !== id));
    toast({ title: "Staff removed" });
  };

  const setStatus = (id: string, status: AvailabilityStatus) => {
    setStaff(staff.map((s) => (s.id === id ? { ...s, status } : s)));
  };

  const toggleBot = (bot: string) => {
    const updated = form.assignedBots.includes(bot)
      ? form.assignedBots.filter((b) => b !== bot)
      : [...form.assignedBots, bot];
    setForm({ ...form, assignedBots: updated });
  };

  const toggleDept = (dept: string) => {
    const updated = form.departments.includes(dept)
      ? form.departments.filter((d) => d !== dept)
      : [...form.departments, dept];
    setForm({ ...form, departments: updated });
  };

  const filtered = staff
    .filter((s) => filterDept === "all" || s.departments.includes(filterDept))
    .filter((s) => filterBot === "all" || s.assignedBots.includes(filterBot));

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Staff Management</h1>
          <p className="text-muted-foreground">Manage team members for notifications and escalations</p>
        </div>
        <Button onClick={openAdd}><Plus className="mr-2 h-4 w-4" />Add Staff</Button>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <Label className="text-sm">Department:</Label>
        <Select value={filterDept} onValueChange={setFilterDept}>
          <SelectTrigger className="w-[160px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Departments</SelectItem>
            {departments.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
          </SelectContent>
        </Select>
        <Label className="text-sm">Bot:</Label>
        <Select value={filterBot} onValueChange={setFilterBot}>
          <SelectTrigger className="w-[160px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Bots</SelectItem>
            {bots.map((b) => <SelectItem key={b} value={b}>{b}</SelectItem>)}
          </SelectContent>
        </Select>
        <span className="text-sm text-muted-foreground">{filtered.length} member{filtered.length !== 1 ? "s" : ""}</span>
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Department</TableHead>
                <TableHead>Bots</TableHead>
                <TableHead>Channels</TableHead>
                <TableHead>Availability</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="w-[100px]">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((s) => (
                <TableRow key={s.id} className={s.status === "offline" ? "opacity-50" : ""}>
                  <TableCell className="font-medium">
                    <div className="flex items-center gap-1.5">
                      <span className={`h-2 w-2 rounded-full ${availabilityStatuses.find((st) => st.value === s.status)?.color}`} />
                      {s.name}
                      {s.escalationLevels.length > 0 && <span title={`Escalation: ${s.escalationLevels.join(", ")}`}><Shield className="h-3.5 w-3.5 text-primary" /></span>}
                    </div>
                  </TableCell>
                  <TableCell>{s.role}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {s.departments.map((d) => (
                        <Badge key={d} variant="secondary" className="text-xs">{d}</Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {s.assignedBots.map((b) => (
                        <Badge key={b} variant="outline" className="text-xs">{b.replace(" Bot", "")}</Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      {s.emailEnabled && <Mail className="h-3.5 w-3.5 text-muted-foreground" />}
                      {s.whatsappEnabled && <Phone className="h-3.5 w-3.5 text-muted-foreground" />}
                      {!s.emailEnabled && !s.whatsappEnabled && <span className="text-xs text-muted-foreground">None</span>}
                    </div>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">{s.availabilityFrom} – {s.availabilityTo}</TableCell>
                  <TableCell>
                    <Select value={s.status} onValueChange={(v: AvailabilityStatus) => setStatus(s.id, v)}>
                      <SelectTrigger className="w-[100px]"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {availabilityStatuses.map((st) => (
                          <SelectItem key={st.value} value={st.value}>
                            <span className="flex items-center gap-1.5">
                              <span className={`h-2 w-2 rounded-full ${st.color}`} />
                              {st.label}
                            </span>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      <Button variant="ghost" size="icon" onClick={() => openEdit(s)}><Pencil className="h-4 w-4" /></Button>
                      <Button variant="ghost" size="icon" onClick={() => remove(s.id)}><Trash2 className="h-4 w-4 text-destructive" /></Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              {filtered.length === 0 && (
                <TableRow><TableCell colSpan={8} className="text-center text-muted-foreground py-8">No staff members found</TableCell></TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editingId ? "Edit Staff Member" : "Add Staff Member"}</DialogTitle>
            <DialogDescription>Fill in the details below.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label>Full Name *</Label>
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
            <div className="space-y-1.5">
              <Label>Role</Label>
              <Select value={form.role} onValueChange={(v) => setForm({ ...form, role: v })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>{roles.map((r) => <SelectItem key={r} value={r}>{r}</SelectItem>)}</SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label>Departments *</Label>
              <div className="flex flex-wrap gap-2">
                {departments.map((dept) => (
                  <Badge
                    key={dept}
                    variant={form.departments.includes(dept) ? "default" : "outline"}
                    className="cursor-pointer select-none"
                    onClick={() => toggleDept(dept)}
                  >
                    {dept}
                  </Badge>
                ))}
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>Assigned Bots *</Label>
              <div className="flex flex-wrap gap-2">
                {bots.map((bot) => (
                  <Badge
                    key={bot}
                    variant={form.assignedBots.includes(bot) ? "default" : "outline"}
                    className="cursor-pointer select-none"
                    onClick={() => toggleBot(bot)}
                  >
                    {bot}
                  </Badge>
                ))}
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>Email *</Label>
              <Input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </div>
            <div className="space-y-1.5">
              <Label>WhatsApp Number</Label>
              <Input value={form.whatsapp} onChange={(e) => setForm({ ...form, whatsapp: e.target.value })} placeholder="+91..." />
            </div>

            <div className="space-y-2">
              <Label>Notification Channels</Label>
              <div className="flex items-center gap-6">
                <label className="flex items-center gap-2 text-sm cursor-pointer">
                  <Switch checked={form.emailEnabled} onCheckedChange={(v) => setForm({ ...form, emailEnabled: v })} />
                  <Mail className="h-3.5 w-3.5" /> Email
                </label>
                <label className="flex items-center gap-2 text-sm cursor-pointer">
                  <Switch checked={form.whatsappEnabled} onCheckedChange={(v) => setForm({ ...form, whatsappEnabled: v })} />
                  <Phone className="h-3.5 w-3.5" /> WhatsApp
                </label>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>Available From</Label>
                <Input type="time" value={form.availabilityFrom} onChange={(e) => setForm({ ...form, availabilityFrom: e.target.value })} />
              </div>
              <div className="space-y-1.5">
                <Label>Available To</Label>
                <Input type="time" value={form.availabilityTo} onChange={(e) => setForm({ ...form, availabilityTo: e.target.value })} />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>Working Days</Label>
              <div className="flex flex-wrap gap-1.5">
                {weekDays.map((day) => (
                  <Badge
                    key={day}
                    variant={form.workingDays.includes(day) ? "default" : "outline"}
                    className="cursor-pointer select-none"
                    onClick={() => {
                      const updated = form.workingDays.includes(day)
                        ? form.workingDays.filter((d) => d !== day)
                        : [...form.workingDays, day];
                      setForm({ ...form, workingDays: updated });
                    }}
                  >
                    {day}
                  </Badge>
                ))}
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>Escalation Levels</Label>
              <p className="text-xs text-muted-foreground">Select which escalation levels this staff member handles</p>
              <div className="flex flex-wrap gap-1.5">
                {escalationLevels.map((level) => (
                  <Badge
                    key={level}
                    variant={form.escalationLevels.includes(level) ? "default" : "outline"}
                    className="cursor-pointer select-none"
                    onClick={() => {
                      const updated = form.escalationLevels.includes(level)
                        ? form.escalationLevels.filter((l) => l !== level)
                        : [...form.escalationLevels, level];
                      setForm({ ...form, escalationLevels: updated });
                    }}
                  >
                    {level}
                  </Badge>
                ))}
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button>
            <Button onClick={save}>{editingId ? "Update" : "Add"}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default StaffManagement;
