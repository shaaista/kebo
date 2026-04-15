import { motion } from "framer-motion";
import { Hotel, ShoppingCart, Building2 } from "lucide-react";

const useCases = [
  {
    icon: Hotel,
    title: "Hotel & Hospitality",
    desc: "Crawl property websites, train the bot on rooms, amenities, and policies for instant guest answers.",
  },
  {
    icon: ShoppingCart,
    title: "E-commerce Support",
    desc: "Ingest product catalogs and FAQs so the bot handles orders, returns, and product questions.",
  },
  {
    icon: Building2,
    title: "Enterprise Helpdesk",
    desc: "Feed internal docs and policies to deploy an always-on HR/IT assistant.",
  },
];

export const ProductUseCases = () => (
  <section className="py-12">
    <div className="mx-auto max-w-6xl px-4">
      <h2 className="mb-4 text-center text-3xl font-bold">Use Cases</h2>
      <p className="mx-auto mb-8 max-w-2xl text-center text-muted-foreground">
        Kebo Bot adapts to any industry that needs intelligent customer support.
      </p>
      <div className="grid gap-6 md:grid-cols-3">
        {useCases.map((uc, i) => (
          <motion.div
            key={uc.title}
            initial={{ opacity: 0, y: 20 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.1, duration: 0.4 }}
            className="rounded-xl border bg-card p-6"
          >
            <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10">
              <uc.icon className="h-6 w-6 text-primary" />
            </div>
            <h3 className="mb-2 text-lg font-semibold">{uc.title}</h3>
            <p className="text-sm text-muted-foreground">{uc.desc}</p>
          </motion.div>
        ))}
      </div>
    </div>
  </section>
);
