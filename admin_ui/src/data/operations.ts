export const bots = ["Website Bot", "WhatsApp Bot", "Booking Bot"];

export const departments = ["Front Desk", "Housekeeping", "F&B", "Maintenance", "Management", "IT", "Guest Relations"];

export const roles = ["Manager", "Supervisor", "Executive", "Agent", "Admin", "Escalation Manager"];

export const roleHierarchy: Record<string, number> = {
  Agent: 1,
  Executive: 2,
  Supervisor: 3,
  Manager: 4,
  "Escalation Manager": 5,
  Admin: 6,
};

export const availabilityStatuses = [
  { value: "online", label: "Online", color: "bg-green-500" },
  { value: "offline", label: "Offline", color: "bg-muted-foreground" },
  { value: "busy", label: "Busy", color: "bg-amber-500" },
] as const;

export type AvailabilityStatus = "online" | "offline" | "busy";

export const triggerTypes = [
  { value: "form-submission", label: "Form Submission" },
  { value: "intent-detected", label: "Intent Detected" },
  { value: "ticket-created", label: "Ticket Created" },
  { value: "sla-warning", label: "SLA Warning" },
  { value: "sla-breach", label: "SLA Breach" },
  { value: "escalation", label: "Escalation" },
  { value: "daily-summary", label: "Daily Summary" },
  { value: "low-rating", label: "Low Rating (≤3)" },
  { value: "manual-request", label: "Manual Request" },
  { value: "no-resolution", label: "No Resolution" },
];

export const consolidationIntervals = [
  { value: "0", label: "Instant (no batching)" },
  { value: "5", label: "Every 5 minutes" },
  { value: "15", label: "Every 15 minutes" },
  { value: "30", label: "Every 30 minutes" },
  { value: "60", label: "Every 1 hour" },
];

export const assignmentMethods = [
  { value: "round-robin", label: "Round Robin", description: "Distribute tickets evenly across available staff" },
  { value: "load-based", label: "Load Based", description: "Assign to agent with fewest active tickets" },
  { value: "skill-based", label: "Skill Based", description: "Match ticket to agent's department expertise" },
  { value: "vip", label: "VIP Priority", description: "Route VIP guests directly to senior staff" },
  { value: "manual", label: "Manual", description: "All tickets go to queue for manual assignment" },
];

export const fallbackBehaviors = [
  { value: "queue", label: "Queue (FIFO)", description: "Hold in queue until an agent is free" },
  { value: "assign-manager", label: "Assign to Manager", description: "Route to department manager" },
  { value: "auto-escalate", label: "Auto-Escalate", description: "Escalate after configured timeout" },
  { value: "bot-continue", label: "Bot Continues", description: "Bot handles basic queries while waiting" },
];

export const ticketStatuses = [
  { value: "open", label: "Open", color: "bg-blue-500" },
  { value: "assigned", label: "Assigned", color: "bg-purple-500" },
  { value: "in-progress", label: "In Progress", color: "bg-amber-500" },
  { value: "on-hold", label: "On Hold", color: "bg-muted-foreground" },
  { value: "resolved", label: "Resolved", color: "bg-green-500" },
  { value: "closed", label: "Closed", color: "bg-muted-foreground" },
];

export interface DepartmentConfig {
  id: string;
  name: string;
  manager: string;
  escalationManager: string;
  workingHoursFrom: string;
  workingHoursTo: string;
  workingDays: string[];
  is24x7: boolean;
  afterHoursBehavior: string;
  afterHoursMessage: string;
  enabled: boolean;
}

export const defaultDepartments: DepartmentConfig[] = [
  { id: "d1", name: "Front Desk", manager: "Priya Sharma", escalationManager: "Priya Sharma", workingHoursFrom: "06:00", workingHoursTo: "22:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], is24x7: false, afterHoursBehavior: "queue", afterHoursMessage: "Our front desk team will respond when we open. For emergencies, please call the hotel directly.", enabled: true },
  { id: "d2", name: "Housekeeping", manager: "Rahul Verma", escalationManager: "Rahul Verma", workingHoursFrom: "07:00", workingHoursTo: "20:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], is24x7: false, afterHoursBehavior: "bot-continue", afterHoursMessage: "Housekeeping requests will be handled first thing in the morning.", enabled: true },
  { id: "d3", name: "F&B", manager: "Vikram Patel", escalationManager: "Vikram Patel", workingHoursFrom: "06:00", workingHoursTo: "23:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], is24x7: false, afterHoursBehavior: "queue", afterHoursMessage: "Our restaurant is currently closed. We'll attend to your request when we reopen.", enabled: true },
  { id: "d4", name: "Maintenance", manager: "", escalationManager: "", workingHoursFrom: "08:00", workingHoursTo: "17:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], is24x7: false, afterHoursBehavior: "auto-escalate", afterHoursMessage: "For urgent maintenance issues, our on-call team has been notified.", enabled: true },
  { id: "d5", name: "Management", manager: "", escalationManager: "", workingHoursFrom: "09:00", workingHoursTo: "18:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri"], is24x7: false, afterHoursBehavior: "queue", afterHoursMessage: "", enabled: true },
  { id: "d6", name: "IT", manager: "", escalationManager: "", workingHoursFrom: "09:00", workingHoursTo: "18:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri"], is24x7: false, afterHoursBehavior: "queue", afterHoursMessage: "", enabled: true },
  { id: "d7", name: "Guest Relations", manager: "", escalationManager: "", workingHoursFrom: "08:00", workingHoursTo: "20:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], is24x7: false, afterHoursBehavior: "bot-continue", afterHoursMessage: "We appreciate your feedback. Our guest relations team will reach out during business hours.", enabled: true },
];

export const weekDays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export const escalationLevels = ["L1", "L2", "L3"] as const;
export type EscalationLevel = (typeof escalationLevels)[number];

export interface StaffMember {
  id: string;
  name: string;
  role: string;
  departments: string[];
  email: string;
  whatsapp: string;
  emailEnabled: boolean;
  whatsappEnabled: boolean;
  availabilityFrom: string;
  availabilityTo: string;
  workingDays: string[];
  assignedBots: string[];
  escalationLevels: EscalationLevel[];
  status: AvailabilityStatus;
}

export const defaultStaff: StaffMember[] = [
  { id: "s1", name: "Priya Sharma", role: "Manager", departments: ["Front Desk", "Guest Relations"], email: "priya@grandhotel.com", whatsapp: "+919876543210", emailEnabled: true, whatsappEnabled: true, availabilityFrom: "09:00", availabilityTo: "18:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], assignedBots: ["Website Bot", "WhatsApp Bot"], escalationLevels: ["L2", "L3"], status: "online" },
  { id: "s2", name: "Rahul Verma", role: "Supervisor", departments: ["Housekeeping"], email: "rahul@grandhotel.com", whatsapp: "+919876543211", emailEnabled: true, whatsappEnabled: true, availabilityFrom: "08:00", availabilityTo: "17:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri"], assignedBots: ["Website Bot"], escalationLevels: ["L1", "L2"], status: "online" },
  { id: "s3", name: "Anita Desai", role: "Executive", departments: ["Front Desk"], email: "anita@grandhotel.com", whatsapp: "+919876543212", emailEnabled: true, whatsappEnabled: false, availabilityFrom: "10:00", availabilityTo: "19:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], assignedBots: ["Website Bot", "Booking Bot"], escalationLevels: ["L1"], status: "busy" },
  { id: "s4", name: "Vikram Patel", role: "Manager", departments: ["F&B", "Maintenance"], email: "vikram@grandhotel.com", whatsapp: "+919876543213", emailEnabled: true, whatsappEnabled: true, availabilityFrom: "07:00", availabilityTo: "16:00", workingDays: ["Mon", "Tue", "Wed", "Thu", "Fri"], assignedBots: ["WhatsApp Bot", "Booking Bot"], escalationLevels: ["L2", "L3"], status: "offline" },
];
