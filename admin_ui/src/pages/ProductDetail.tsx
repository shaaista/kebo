import { motion } from "framer-motion";
import { Link, useNavigate } from "react-router-dom";
import { ProductFlow } from "@/components/product/ProductFlow";
import { ProductFeatures } from "@/components/product/ProductFeatures";
import { ProductStats } from "@/components/product/ProductStats";
import { ProductUseCases } from "@/components/product/ProductUseCases";
import { NovaBotDemo } from "@/components/product/NovaBotDemo";
import { ThemeToggle } from "@/components/ThemeToggle";
import { NeorBotWidget } from "@/components/chatbot/NeorBotWidget";
import { useThemeLogo } from "@/hooks/use-theme-logo";

const ProductDetail = () => {
  const nexoriaLogo = useThemeLogo();
  return (
    <div className="min-h-screen">
      {/* Topbar */}
      <header className="sticky top-0 z-50 border-b bg-background/80 backdrop-blur-md">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4">
          <img src={nexoriaLogo} alt="Nexoria" className="h-7 w-auto" />
          <div className="flex items-center gap-3">
            <ThemeToggle />
            <Link
              to="/app/dashboard"
              className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              Get Started
            </Link>
          </div>
        </div>
      </header>

      {/* Hero */}
      <section className="py-12 md:py-16">
        <div className="mx-auto max-w-4xl px-4 text-center">
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
            <span className="mb-4 inline-block rounded-full bg-primary/10 px-4 py-1.5 text-sm font-medium text-primary">
              Kebo Bot — AI-Powered Customer Intelligence
            </span>
            <h1 className="mb-4 text-4xl font-bold tracking-tight md:text-5xl lg:text-6xl">
              AI Chatbot with Smart{" "}
              <span className="text-primary">Knowledge Ingestion</span>
            </h1>
            <p className="mx-auto mb-6 max-w-2xl text-lg text-muted-foreground">
              Crawl any website, curate the content you want, and deploy an intelligent chatbot
              that answers from your actual data — in minutes, not months.
            </p>
            <div className="flex flex-wrap items-center justify-center gap-4">
              <Link
                to="/app/dashboard"
                className="rounded-lg bg-primary px-6 py-3 font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                Get Started
              </Link>
              <a
                href="#demo"
                className="rounded-lg border px-6 py-3 font-medium transition-colors hover:bg-accent"
              >
                Watch Demo
              </a>
            </div>
          </motion.div>
        </div>
      </section>

      <ProductStats />
      <ProductFlow />
      <div id="demo">
        <NovaBotDemo />
      </div>
      <ProductFeatures />
      <ProductUseCases />

      {/* CTA */}
      <section className="bg-primary py-10">
        <div className="mx-auto max-w-3xl px-4 text-center">
          <h2 className="mb-4 text-3xl font-bold text-primary-foreground">Ready to Get Started?</h2>
          <p className="mb-8 text-primary-foreground/80">
            Turn your website into an AI-powered knowledge base in minutes.
          </p>
          <Link
            to="/app/dashboard"
            className="inline-block rounded-lg bg-background px-8 py-3 font-medium text-foreground transition-colors hover:bg-background/90"
          >
            Get Started
          </Link>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t bg-card py-8">
        <div className="mx-auto max-w-6xl px-4 text-center">
          <img src={nexoriaLogo} alt="Nexoria" className="mx-auto mb-3 h-6 w-auto" />
          <p className="text-xs text-muted-foreground">
            © {new Date().getFullYear()} Nexoria. All rights reserved.
          </p>
        </div>
      </footer>

      <NeorBotWidget botName="Kebo" brandColor="#C72C41" position="right" />
    </div>
  );
};

export default ProductDetail;
