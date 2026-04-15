import { Link } from "react-router-dom";
import { ChevronDown, Menu, X, Bot } from "lucide-react";
import { useState } from "react";
import { ThemeToggle } from "./ThemeToggle";
import { useThemeLogo } from "@/hooks/use-theme-logo";

const products = [
  {
    name: "Kebo Bot",
    description: "AI Chatbot & Knowledge Manager",
    href: "/products/neor-bot",
    icon: Bot,
  },
];

export const Navbar = () => {
  const nexoriaLogo = useThemeLogo();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [productsOpen, setProductsOpen] = useState(false);

  return (
    <nav className="sticky top-0 z-50 border-b bg-background/80 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4">
        {/* Logo */}
        <Link to="/" className="flex items-center gap-3">
          <img src={nexoriaLogo} alt="Nexoria" className="h-8 w-auto" />
          <span className="hidden text-xs font-medium text-muted-foreground lg:inline">
            AI-Powered Customer Intelligence
          </span>
        </Link>

        {/* Desktop nav */}
        <div className="hidden items-center gap-6 md:flex">
          <div className="relative">
            <button
              onClick={() => setProductsOpen(!productsOpen)}
              className="flex items-center gap-1 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
            >
              Products <ChevronDown className="h-4 w-4" />
            </button>
            {productsOpen && (
              <>
                <div className="fixed inset-0" onClick={() => setProductsOpen(false)} />
                <div className="absolute left-0 top-full mt-2 w-72 rounded-lg border bg-popover p-2 shadow-lg">
                  {products.map((p) => (
                    <Link
                      key={p.href}
                      to={p.href}
                      onClick={() => setProductsOpen(false)}
                      className="flex items-start gap-3 rounded-md p-3 transition-colors hover:bg-accent"
                    >
                      <p.icon className="mt-0.5 h-5 w-5 text-primary" />
                      <div>
                        <div className="text-sm font-medium">{p.name}</div>
                        <div className="text-xs text-muted-foreground">{p.description}</div>
                      </div>
                    </Link>
                  ))}
                </div>
              </>
            )}
          </div>
          <Link to="/about" className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground">
            About
          </Link>
          <ThemeToggle />
          <Link
            to="/products/neor-bot"
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Get Started
          </Link>
        </div>

        {/* Mobile */}
        <div className="flex items-center gap-2 md:hidden">
          <ThemeToggle />
          <button onClick={() => setMobileOpen(!mobileOpen)} aria-label="Menu">
            {mobileOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
          </button>
        </div>
      </div>

      {/* Mobile menu */}
      {mobileOpen && (
        <div className="border-t bg-background p-4 md:hidden">
          <div className="flex flex-col gap-3">
            {products.map((p) => (
              <Link
                key={p.href}
                to={p.href}
                onClick={() => setMobileOpen(false)}
                className="flex items-center gap-2 text-sm font-medium"
              >
                <p.icon className="h-4 w-4 text-primary" />
                {p.name}
              </Link>
            ))}
            <Link to="/about" onClick={() => setMobileOpen(false)} className="text-sm font-medium">
              About
            </Link>
            <Link
              to="/products/neor-bot"
              onClick={() => setMobileOpen(false)}
              className="mt-2 rounded-md bg-primary px-4 py-2 text-center text-sm font-medium text-primary-foreground"
            >
              Get Started
            </Link>
          </div>
        </div>
      )}
    </nav>
  );
};
