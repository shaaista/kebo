import { motion } from "framer-motion";
import { Link } from "react-router-dom";
import { Bot, ArrowRight, Globe, MessageSquare, Zap, Shield } from "lucide-react";

const Index = () => (
  <>
    {/* Hero — Split Layout */}
    <section className="py-16 md:py-24 overflow-hidden">
      <div className="mx-auto max-w-6xl px-4">
        <div className="grid items-center gap-10 lg:grid-cols-2">
          {/* Left — Text */}
          <motion.div
            initial={{ opacity: 0, x: -30 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.6 }}
          >
            <span className="mb-4 inline-block rounded-full border border-primary/20 bg-primary/5 px-4 py-1.5 text-xs font-semibold text-primary">
              AI-Powered Customer Intelligence
            </span>
            <h1 className="mb-5 text-4xl font-bold tracking-tight md:text-5xl lg:text-[3.25rem] lg:leading-tight">
              Intelligent Solutions for{" "}
              <span className="text-primary">Modern Businesses</span>
            </h1>
            <p className="mb-6 max-w-lg text-lg text-muted-foreground">
              Nexoria builds AI-powered products that help businesses automate support,
              manage knowledge, and engage customers at scale.
            </p>
            <div className="flex flex-wrap gap-3">
              <Link
                to="/products/neor-bot"
                className="inline-flex items-center gap-2 rounded-lg bg-primary px-6 py-3 font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                Explore Kebo Bot <ArrowRight className="h-4 w-4" />
              </Link>
              <a
                href="#demo"
                className="inline-flex items-center gap-2 rounded-lg border px-6 py-3 font-medium text-foreground transition-colors hover:bg-muted"
              >
                Watch Demo
              </a>
            </div>
            {/* Quick stats */}
            <div className="mt-8 flex gap-8">
              {[
                { label: "Response Time", value: "<2s" },
                { label: "Accuracy", value: "95%+" },
                { label: "Languages", value: "50+" },
              ].map((s) => (
                <div key={s.label}>
                  <div className="text-2xl font-bold text-primary">{s.value}</div>
                  <div className="text-xs text-muted-foreground">{s.label}</div>
                </div>
              ))}
            </div>
          </motion.div>

          {/* Right — Hero Illustration */}
          <motion.div
            initial={{ opacity: 0, x: 30 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.6, delay: 0.2 }}
            className="relative"
          >
            <div className="absolute -right-10 -top-10 h-64 w-64 rounded-full bg-primary/5 blur-3xl" />
            <div className="absolute -bottom-10 -left-10 h-48 w-48 rounded-full bg-primary/10 blur-3xl" />
            <img
              src="/images/hero-illustration.jpg"
              alt="AI-powered chatbot dashboard with analytics and conversation management"
              width={1024}
              height={768}
              className="relative rounded-xl shadow-2xl border"
            />
          </motion.div>
        </div>
      </div>
    </section>

    {/* Features grid */}
    <section className="border-t bg-muted/50 py-16">
      <div className="mx-auto max-w-6xl px-4">
        <h2 className="mb-10 text-center text-3xl font-bold">Why Kebo Bot?</h2>
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {[
            { icon: Globe, title: "Website Crawling", desc: "Automatically extract and index your entire website content" },
            { icon: Bot, title: "AI Chatbot", desc: "Deploy an intelligent bot trained on your actual business data" },
            { icon: Zap, title: "Instant Answers", desc: "Sub-second responses powered by advanced RAG pipeline" },
            { icon: Shield, title: "Smart Escalation", desc: "Seamless handoff to human agents when needed" },
          ].map((f) => (
            <motion.div
              key={f.title}
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              className="rounded-xl border bg-card p-5"
            >
              <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                <f.icon className="h-5 w-5 text-primary" />
              </div>
              <h3 className="mb-1 font-semibold">{f.title}</h3>
              <p className="text-sm text-muted-foreground">{f.desc}</p>
            </motion.div>
          ))}
        </div>
        <div className="mt-8 text-center">
          <Link
            to="/products/neor-bot"
            className="inline-flex items-center gap-2 text-sm font-medium text-primary hover:underline"
          >
            Explore all features <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      </div>
    </section>
  </>
);

export default Index;
