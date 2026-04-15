import { Link } from "react-router-dom";
import { useThemeLogo } from "@/hooks/use-theme-logo";

export const Footer = () => {
  const nexoriaLogo = useThemeLogo();
  return (
  <footer className="border-t bg-card">
    <div className="mx-auto max-w-6xl px-4 py-12">
      <div className="grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <img src={nexoriaLogo} alt="Nexoria" className="mb-4 h-8 w-auto" />
          <p className="text-sm text-muted-foreground">
            AI-Powered Growth Engine
          </p>
        </div>
        <div>
          <h4 className="mb-3 text-sm font-semibold">Products</h4>
          <ul className="space-y-2">
            <li>
              <Link to="/products/neor-bot" className="text-sm text-muted-foreground hover:text-foreground">
                Kebo Bot
              </Link>
            </li>
          </ul>
        </div>
        <div>
          <h4 className="mb-3 text-sm font-semibold">Company</h4>
          <ul className="space-y-2">
            <li>
              <Link to="/about" className="text-sm text-muted-foreground hover:text-foreground">
                About
              </Link>
            </li>
          </ul>
        </div>
        <div>
          <h4 className="mb-3 text-sm font-semibold">Connect</h4>
          <ul className="space-y-2">
            <li>
              <a href="mailto:hello@nexoria.com" className="text-sm text-muted-foreground hover:text-foreground">
                hello@nexoria.com
              </a>
            </li>
          </ul>
        </div>
      </div>
      <div className="mt-8 border-t pt-8 text-center text-xs text-muted-foreground">
        © {new Date().getFullYear()} Nexoria. All rights reserved.
      </div>
    </div>
  </footer>
  );
};
