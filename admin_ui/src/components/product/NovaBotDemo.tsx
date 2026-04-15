import { useState, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Check, X, Bot, User, Play } from "lucide-react";
import { Progress } from "@/components/ui/progress";

/* ── Content Manager Data ── */
const tabs = ["Pages", "Images", "Videos", "Files"] as const;
type Tab = (typeof tabs)[number];

const sampleItems: Record<Tab, { name: string; included: boolean }[]> = {
  Pages: [
    { name: "/about-us", included: true },
    { name: "/rooms/deluxe-suite", included: true },
    { name: "/careers", included: false },
    { name: "/contact", included: true },
    { name: "/spa-services", included: true },
  ],
  Images: [
    { name: "hero-banner.jpg", included: true },
    { name: "lobby-photo.png", included: true },
    { name: "temp-banner.jpg", included: false },
  ],
  Videos: [
    { name: "property-tour.mp4", included: true },
    { name: "promo-2023.mp4", included: false },
  ],
  Files: [
    { name: "room-rates.pdf", included: true },
    { name: "menu.pdf", included: true },
    { name: "old-brochure.pdf", included: false },
  ],
};

const CONTENT_CYCLE_MS = 2500;
const CONTENT_TOTAL_MS = CONTENT_CYCLE_MS * tabs.length;

/* ── Chat Data ── */
const chatScript: { role: "user" | "bot"; text: string }[] = [
  { role: "user", text: "What amenities does the Deluxe Suite include?" },
  { role: "bot", text: "The Deluxe Suite features a king-size bed, private balcony, minibar, rain shower, and complimentary breakfast. Shall I help you book?" },
  { role: "user", text: "What's your check-in time?" },
  { role: "bot", text: "Check-in is from 2:00 PM. Early check-in is available on request, subject to availability. Would you like me to arrange that?" },
];

const CHAT_TOTAL_MS = chatScript.length * 2000 + 3000;

/* ── Typing Indicator ── */
const TypingDots = () => (
  <div className="flex gap-2 items-center">
    <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/10">
      <Bot className="h-3 w-3 text-primary" />
    </div>
    <div className="flex gap-1 rounded-lg bg-muted px-3 py-2.5">
      {[0, 1, 2].map((i) => (
        <motion.div
          key={i}
          className="h-1.5 w-1.5 rounded-full bg-muted-foreground/50"
          animate={{ y: [0, -4, 0] }}
          transition={{ duration: 0.6, repeat: Infinity, delay: i * 0.15 }}
        />
      ))}
    </div>
  </div>
);

/* ── Looping Progress Bar ── */
const LoopingProgress = ({ durationMs }: { durationMs: number }) => {
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    const interval = 50;
    const step = (100 / durationMs) * interval;
    const timer = setInterval(() => {
      setProgress((prev) => (prev >= 100 ? 0 : prev + step));
    }, interval);
    return () => clearInterval(timer);
  }, [durationMs]);

  return <Progress value={progress} className="h-1 rounded-none" />;
};

/* ── Video Frame Wrapper ── */
const VideoFrame = ({
  children,
  durationMs,
}: {
  children: React.ReactNode;
  durationMs: number;
}) => {
  const [showPlay, setShowPlay] = useState(true);

  useEffect(() => {
    const t = setTimeout(() => setShowPlay(false), 1500);
    return () => clearTimeout(t);
  }, []);

  return (
    <div className="relative rounded-xl border-2 border-border bg-card shadow-lg overflow-hidden">
      {/* Fading play overlay */}
      <AnimatePresence>
        {showPlay && (
          <motion.div
            initial={{ opacity: 0.7 }}
            animate={{ opacity: 0.7 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.6 }}
            className="absolute inset-0 z-10 flex items-center justify-center bg-background/40 backdrop-blur-sm"
          >
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/90 text-primary-foreground shadow-md">
              <Play className="h-5 w-5 ml-0.5" />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      {children}
      <LoopingProgress durationMs={durationMs} />
    </div>
  );
};

/* ── Content Manager Animated Card ── */
const ContentManagerCard = () => {
  const [activeTab, setActiveTab] = useState<Tab>("Pages");

  useEffect(() => {
    const interval = setInterval(() => {
      setActiveTab((prev) => {
        const idx = tabs.indexOf(prev);
        return tabs[(idx + 1) % tabs.length];
      });
    }, CONTENT_CYCLE_MS);
    return () => clearInterval(interval);
  }, []);

  return (
    <VideoFrame durationMs={CONTENT_TOTAL_MS}>
      <div className="border-b p-4">
        <h3 className="text-sm font-semibold">Content Manager</h3>
        <p className="text-[10px] text-muted-foreground">Auto-curate what your bot learns</p>
      </div>
      <div className="flex border-b">
        {tabs.map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`flex-1 px-3 py-2 text-xs font-medium transition-colors relative ${
              activeTab === tab
                ? "text-primary"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab}
            {activeTab === tab && (
              <motion.div
                layoutId="content-tab-indicator"
                className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary"
              />
            )}
          </button>
        ))}
      </div>
      <div className="p-2 min-h-[220px]">
        <AnimatePresence mode="wait">
          <motion.div
            key={activeTab}
            initial={{ opacity: 0, x: 10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -10 }}
            transition={{ duration: 0.2 }}
            className="divide-y"
          >
            {sampleItems[activeTab].map((item, i) => (
              <motion.div
                key={item.name}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.08 }}
                className="flex items-center justify-between px-2 py-2"
              >
                <span className="truncate text-xs">{item.name}</span>
                <motion.span
                  initial={{ scale: 0.5 }}
                  animate={{ scale: 1 }}
                  transition={{ delay: i * 0.08 + 0.1 }}
                  className={`flex h-5 w-5 items-center justify-center rounded-full ${
                    item.included
                      ? "bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400"
                      : "bg-red-100 text-red-500 dark:bg-red-900/30 dark:text-red-400"
                  }`}
                >
                  {item.included ? <Check className="h-3 w-3" /> : <X className="h-3 w-3" />}
                </motion.span>
              </motion.div>
            ))}
          </motion.div>
        </AnimatePresence>
      </div>
    </VideoFrame>
  );
};

/* ── Chat Animated Card ── */
const ChatCard = () => {
  const [visibleMessages, setVisibleMessages] = useState<number>(0);
  const [showTyping, setShowTyping] = useState(false);

  useEffect(() => {
    let timeouts: ReturnType<typeof setTimeout>[] = [];
    const runSequence = () => {
      setVisibleMessages(0);
      setShowTyping(false);

      chatScript.forEach((msg, i) => {
        if (msg.role === "user") {
          timeouts.push(setTimeout(() => {
            setShowTyping(false);
            setVisibleMessages(i + 1);
          }, i * 2000));
        } else {
          timeouts.push(setTimeout(() => {
            setShowTyping(true);
          }, i * 2000 - 800));
          timeouts.push(setTimeout(() => {
            setShowTyping(false);
            setVisibleMessages(i + 1);
          }, i * 2000));
        }
      });

      timeouts.push(setTimeout(() => {
        setVisibleMessages(0);
        setShowTyping(false);
        setTimeout(runSequence, 500);
      }, chatScript.length * 2000 + 3000));
    };

    runSequence();
    return () => timeouts.forEach(clearTimeout);
  }, []);

  return (
    <VideoFrame durationMs={CHAT_TOTAL_MS}>
      <div className="flex items-center gap-2 border-b p-4">
        <Bot className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-semibold">Kebo Bot Chat</h3>
        <span className="ml-auto rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/30 dark:text-green-400">
          Online
        </span>
      </div>
      <div className="space-y-3 p-4 min-h-[220px]">
        <AnimatePresence>
          {chatScript.slice(0, visibleMessages).map((msg, i) => (
            <motion.div
              key={`${msg.role}-${i}`}
              initial={{ opacity: 0, y: 10, scale: 0.95 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -5 }}
              transition={{ duration: 0.3 }}
              className={`flex gap-2 ${msg.role === "user" ? "justify-end" : ""}`}
            >
              {msg.role === "bot" && (
                <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/10">
                  <Bot className="h-3 w-3 text-primary" />
                </div>
              )}
              <div
                className={`max-w-[85%] rounded-lg px-3 py-2 text-xs ${
                  msg.role === "user"
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted"
                }`}
              >
                {msg.text}
              </div>
              {msg.role === "user" && (
                <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted">
                  <User className="h-3 w-3" />
                </div>
              )}
            </motion.div>
          ))}
        </AnimatePresence>
        {showTyping && <TypingDots />}
      </div>
    </VideoFrame>
  );
};

/* ── Main Export ── */
export const NovaBotDemo = () => {
  return (
    <section id="demo" className="bg-muted/50 py-12">
      <div className="mx-auto max-w-6xl px-4">
        <h2 className="mb-4 text-center text-3xl font-bold">See It in Action</h2>
        <p className="mx-auto mb-8 max-w-2xl text-center text-muted-foreground">
          Content flows from your website through curation into intelligent conversations.
        </p>

        {/* Row 1: Image left, Content Curation right */}
        <div className="mb-10 grid items-center gap-6 lg:grid-cols-2 lg:gap-8">
          <motion.div
            initial={{ opacity: 0, x: -20 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true }}
          >
            <img
              src="/images/content-curation.jpg"
              alt="Content curation and filtering illustration"
              loading="lazy"
              width={800}
              height={512}
              className="rounded-xl shadow-md"
            />
          </motion.div>
          <motion.div
            initial={{ opacity: 0, x: 20 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true }}
            className="flex flex-col"
          >
            <h3 className="mb-2 text-xl font-semibold">Smart Content Curation</h3>
            <p className="mb-6 text-sm text-muted-foreground">
              Automatically crawl, filter, and curate your website content. Choose exactly what your bot learns from.
            </p>
            <ContentManagerCard />
          </motion.div>
        </div>

        {/* Row 2: Chat left, Image right */}
        <div className="grid items-center gap-8 lg:grid-cols-2 lg:gap-12">
          <motion.div
            initial={{ opacity: 0, x: -20 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true }}
            className="flex flex-col lg:order-1"
          >
            <h3 className="mb-2 text-xl font-semibold">Intelligent Conversations</h3>
            <p className="mb-6 text-sm text-muted-foreground">
              Your bot answers from real data — not generic responses. Watch it handle customer queries naturally.
            </p>
            <ChatCard />
          </motion.div>
          <motion.div
            initial={{ opacity: 0, x: 20 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true }}
            className="lg:order-2"
          >
            <img
              src="/images/chat-support.jpg"
              alt="AI chatbot customer support illustration"
              loading="lazy"
              width={800}
              height={512}
              className="rounded-xl shadow-md"
            />
          </motion.div>
        </div>
      </div>
    </section>
  );
};
