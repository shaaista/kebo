import {
  LayoutDashboard,
  Globe,
  FileText,
  Bot,
  ClipboardList,
  MessageSquare,
  Briefcase,
  GitBranch,
  Database,
  FlaskConical,
  AlertTriangle,
  SlidersHorizontal,
  Users,
  Bell,
  
  Building2,
} from "lucide-react";
import { NavLink } from "@/components/NavLink";
import { useLocation } from "react-router-dom";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar";
import { useThemeLogo } from "@/hooks/use-theme-logo";

const mainItems = [
  { title: "Dashboard", url: "/app/dashboard", icon: LayoutDashboard },
  { title: "Web Crawling", url: "/app/crawl", icon: Globe },
  { title: "Content Manager", url: "/app/content", icon: FileText },
  { title: "Forms & Feedback", url: "/app/forms", icon: ClipboardList },
];

const trainingItems = [
  { title: "Setup Wizard", url: "/app/training", icon: Bot },
  { title: "RAG", url: "/app/training?tab=rag", icon: Database },
  { title: "Phases", url: "/app/training?tab=phases", icon: GitBranch },
  { title: "Services", url: "/app/training?tab=services", icon: Briefcase },
  { title: "FAQ", url: "/app/training?tab=faq", icon: MessageSquare },
  { title: "Evaluation", url: "/app/training?tab=evaluation", icon: FlaskConical },
  { title: "Escalation", url: "/app/training?tab=escalation", icon: AlertTriangle },
  { title: "Advanced", url: "/app/training?tab=advanced", icon: SlidersHorizontal },
];

const operationsItems = [
  { title: "Staff Management", url: "/app/staff", icon: Users },
  { title: "Departments", url: "/app/departments", icon: Building2 },
  { title: "Notification Rules", url: "/app/notifications", icon: Bell },
  { title: "Escalation Matrix", url: "/app/escalation", icon: AlertTriangle },
  
];

export function AppSidebar() {
  const { state } = useSidebar();
  const nexoriaLogo = useThemeLogo();
  const collapsed = state === "collapsed";
  const location = useLocation();
  const currentUrl = location.pathname + location.search;

  const isTrainingActive = (url: string) => {
    if (url === "/app/training") {
      return location.pathname === "/app/training" && !location.search;
    }
    return currentUrl === url;
  };

  return (
    <Sidebar collapsible="icon">
      <SidebarContent>
        <div className="flex h-14 items-center border-b px-4">
          <img src={nexoriaLogo} alt="Nexoria" className={collapsed ? "h-6 w-auto" : "h-7 w-auto"} />
        </div>
        <SidebarGroup>
          <SidebarGroupLabel>Kebo Bot</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {mainItems.map((item) => (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton asChild>
                    <NavLink to={item.url} end className="hover:bg-muted/50" activeClassName="bg-muted text-primary font-medium">
                      <item.icon className="mr-2 h-4 w-4" />
                      {!collapsed && <span>{item.title}</span>}
                    </NavLink>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel>Bot Training</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {trainingItems.map((item) => (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton asChild>
                    <NavLink
                      to={item.url}
                      end
                      className={`hover:bg-muted/50 ${isTrainingActive(item.url) ? "bg-muted text-primary font-medium" : ""}`}
                      activeClassName=""
                    >
                      <item.icon className="mr-2 h-4 w-4" />
                      {!collapsed && <span>{item.title}</span>}
                    </NavLink>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarGroup>
          <SidebarGroupLabel>Operations</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {operationsItems.map((item) => (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton asChild>
                    <NavLink to={item.url} end className="hover:bg-muted/50" activeClassName="bg-muted text-primary font-medium">
                      <item.icon className="mr-2 h-4 w-4" />
                      {!collapsed && <span>{item.title}</span>}
                    </NavLink>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  );
}
