import { motion } from "framer-motion";
import { Globe, FolderSearch, Database, MessageSquare, UserCheck, Languages } from "lucide-react";

const features = [
  {
    icon: Globe,
    title: "Website Crawler",
    desc: "Paste a URL and Kebo Bot crawls your entire site, respecting robots.txt and sitemaps.",
  },
  {
    icon: FolderSearch,
    title: "Content Curation",
    desc: "Review pages, images, videos, and files in organized tabs. Include, exclude, or label content before training.",
  },
  {
    icon: Database,
    title: "Auto Knowledge Base",
    desc: "Curated content is chunked, embedded, and indexed into a vector store automatically.",
  },
  {
    icon: MessageSquare,
    title: "Natural Conversations",
    desc: "Advanced NLP delivers human-like responses that understand context and intent.",
  },
  {
    icon: UserCheck,
    title: "Smart Escalation",
    desc: "Seamlessly hands off to a human agent when the conversation requires it.",
  },
  {
    icon: Languages,
    title: "30+ Languages",
    desc: "Communicate with customers in their preferred language with built-in multilingual support.",
  },
];

export const ProductFeatures = () => (
  <section className="bg-muted/50 py-12">
    <div className="mx-auto max-w-6xl px-4">
      <h2 className="mb-4 text-center text-3xl font-bold">Features</h2>
      <p className="mx-auto mb-8 max-w-2xl text-center text-muted-foreground">
        Everything you need to build an intelligent, content-aware chatbot.
      </p>
      <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {features.map((f, i) => (
          <motion.div
            key={f.title}
            initial={{ opacity: 0, y: 20 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.05, duration: 0.35 }}
            className="rounded-xl border bg-card p-6 transition-shadow hover:shadow-md"
          >
            <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
              <f.icon className="h-5 w-5 text-primary" />
            </div>
            <h3 className="mb-2 font-semibold">{f.title}</h3>
            <p className="text-sm text-muted-foreground">{f.desc}</p>
          </motion.div>
        ))}
      </div>
    </div>
  </section>
);
