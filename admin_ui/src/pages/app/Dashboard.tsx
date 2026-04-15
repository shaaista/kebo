import { Globe, FileText, Bot, CheckCircle2, AlertCircle, Clock } from "lucide-react";
import { Link } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const stats = [
  { label: "Pages Crawled", value: "142", icon: Globe, color: "text-blue-500" },
  { label: "Content Items", value: "387", icon: FileText, color: "text-primary" },
  { label: "Bot Status", value: "Active", icon: Bot, color: "text-green-500" },
  { label: "Pending Review", value: "23", icon: Clock, color: "text-yellow-500" },
];

const recentActivity = [
  { text: "Website crawl completed for grandhotel.com", time: "2 hours ago", icon: CheckCircle2, status: "success" },
  { text: "15 new pages awaiting content review", time: "3 hours ago", icon: AlertCircle, status: "warning" },
  { text: "Bot training updated with 42 new entries", time: "5 hours ago", icon: CheckCircle2, status: "success" },
  { text: "Feedback form submitted by guest", time: "1 day ago", icon: CheckCircle2, status: "success" },
];

const quickLinks = [
  { label: "Start New Crawl", href: "/app/crawl", icon: Globe },
  { label: "Review Content", href: "/app/content", icon: FileText },
  { label: "Configure Bot", href: "/app/training", icon: Bot },
];

const Dashboard = () => (
  <div className="mx-auto max-w-6xl space-y-6">
    <div>
      <h1 className="text-2xl font-bold">Dashboard</h1>
      <p className="text-muted-foreground">Overview of your Kebo Bot setup</p>
    </div>

    {/* Stats */}
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {stats.map((s) => (
        <Card key={s.label}>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">{s.label}</CardTitle>
            <s.icon className={`h-4 w-4 ${s.color}`} />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{s.value}</div>
          </CardContent>
        </Card>
      ))}
    </div>

    <div className="grid gap-6 lg:grid-cols-2">
      {/* Recent Activity */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Recent Activity</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {recentActivity.map((a, i) => (
            <div key={i} className="flex items-start gap-3">
              <a.icon className={`mt-0.5 h-4 w-4 shrink-0 ${a.status === "success" ? "text-green-500" : "text-yellow-500"}`} />
              <div className="min-w-0 flex-1">
                <p className="text-sm">{a.text}</p>
                <p className="text-xs text-muted-foreground">{a.time}</p>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Quick Links */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Quick Actions</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {quickLinks.map((l) => (
            <Link
              key={l.label}
              to={l.href}
              className="flex items-center gap-3 rounded-lg border p-3 transition-colors hover:bg-accent"
            >
              <l.icon className="h-5 w-5 text-primary" />
              <span className="text-sm font-medium">{l.label}</span>
            </Link>
          ))}
        </CardContent>
      </Card>
    </div>
  </div>
);

export default Dashboard;
