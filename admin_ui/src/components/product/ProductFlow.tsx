import { motion } from "framer-motion";
import { Link2, ClipboardList, Rocket, MessageCircle } from "lucide-react";

const steps = [
  {
    icon: Link2,
    emoji: "🔗",
    title: "Paste Your URL",
    desc: "Enter your website URL. Kebo Bot's crawler finds every page, image, video, and document.",
  },
  {
    icon: ClipboardList,
    emoji: "📋",
    title: "Review & Curate",
    desc: "Browse crawled content by type. Include what matters, exclude what doesn't, label unnamed items.",
  },
  {
    icon: Rocket,
    emoji: "🚀",
    title: "Publish to AI",
    desc: "One click publishes curated content to the knowledge base — chunked, embedded, and indexed.",
  },
  {
    icon: MessageCircle,
    emoji: "💬",
    title: "Bot Goes Live",
    desc: "Deploy on your website, WhatsApp, or app. The bot answers from your actual content, 24/7.",
  },
];

export const ProductFlow = () => (
  <section className="py-12">
    <div className="mx-auto max-w-6xl px-4">
      <h2 className="mb-4 text-center text-3xl font-bold">How It Works</h2>
      <p className="mx-auto mb-8 max-w-2xl text-center text-muted-foreground">
        From website to intelligent chatbot in four simple steps.
      </p>
      <div className="grid gap-8 md:grid-cols-4">
        {steps.map((step, i) => (
          <motion.div
            key={step.title}
            initial={{ opacity: 0, y: 24 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.1, duration: 0.4 }}
            className="relative text-center"
          >
            {i < steps.length - 1 && (
              <div className="absolute right-0 top-8 hidden h-px w-full translate-x-1/2 bg-border md:block" />
            )}
            <div className="relative mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
              <span className="text-2xl">{step.emoji}</span>
            </div>
            <div className="mb-1 text-xs font-semibold text-primary">Step {i + 1}</div>
            <h3 className="mb-2 text-lg font-semibold">{step.title}</h3>
            <p className="text-sm text-muted-foreground">{step.desc}</p>
          </motion.div>
        ))}
      </div>
    </div>
  </section>
);
