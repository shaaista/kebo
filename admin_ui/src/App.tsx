import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import ProductDetail from "./pages/ProductDetail";
import AppLayout from "./components/AppLayout";
import Dashboard from "./pages/app/Dashboard";
import WebCrawl from "./pages/app/WebCrawl";
import ContentManager from "./pages/app/ContentManager";
import BotTraining from "./pages/app/BotTraining";
import FormsDesign from "./pages/app/FormsDesign";
import StaffManagement from "./pages/app/StaffManagement";
import NotificationRules from "./pages/app/NotificationRules";
import EscalationMatrix from "./pages/app/EscalationMatrix";
import Departments from "./pages/app/Departments";
import Widget from "./pages/app/Widget";
import NotFound from "./pages/NotFound";

const queryClient = new QueryClient();

const App = () => (
  <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <Toaster />
      <Sonner />
      <BrowserRouter
        basename="/admin"
        future={{
          v7_relativeSplatPath: true,
          v7_startTransition: true,
        }}
      >
        <Routes>
          <Route path="/" element={<Navigate to="/app/dashboard" replace />} />
          <Route path="/products/neor-bot" element={<ProductDetail />} />
          <Route path="/app" element={<AppLayout />}>
            <Route index element={<Navigate to="dashboard" replace />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="crawl" element={<WebCrawl />} />
            <Route path="content" element={<ContentManager />} />
            <Route path="training" element={<BotTraining />} />
            <Route path="forms" element={<FormsDesign />} />
            <Route path="staff" element={<StaffManagement />} />
            <Route path="departments" element={<Departments />} />
            <Route path="notifications" element={<NotificationRules />} />
            <Route path="escalation" element={<EscalationMatrix />} />
            <Route path="widget" element={<Widget />} />
          </Route>
          <Route path="*" element={<NotFound />} />
        </Routes>
      </BrowserRouter>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
