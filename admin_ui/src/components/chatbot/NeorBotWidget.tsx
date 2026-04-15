import { useState, useEffect } from "react";
import { X, Maximize2, Minus, Plus, MessageCircle } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { ChatInterface } from "./ChatInterface";
import { useIsMobile } from "@/hooks/use-mobile";

interface NeorBotWidgetProps {
  botName?: string;
  brandColor?: string;
  position?: "right" | "left";
  widgetWidth?: number;
  widgetHeight?: number;
}

const DEFAULT_WIDTH = 380;
const DEFAULT_HEIGHT = 560;
const EXPANDED_WIDTH = 560;
const EXPANDED_HEIGHT = 750;

export function NeorBotWidget({
  botName = "Kebo",
  brandColor = "#C72C41",
  position = "right",
  widgetWidth = DEFAULT_WIDTH,
  widgetHeight = DEFAULT_HEIGHT,
}: NeorBotWidgetProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [isMinimized, setIsMinimized] = useState(false);
  const [showTeaser, setShowTeaser] = useState(true);
  const [isExpanded, setIsExpanded] = useState(false);
  const isMobile = useIsMobile();

  const greeting = `Welcome! I'm ${botName} — your AI assistant. How can I help you today?`;
  const positionClass = position === "right" ? "right-4" : "left-4";

  // Auto-show teaser after 3s
  useEffect(() => {
    const timer = setTimeout(() => setShowTeaser(true), 3000);
    return () => clearTimeout(timer);
  }, []);

  const maxHeight = typeof window !== "undefined" ? window.innerHeight - 48 : 700;
  const targetWidth = isExpanded ? EXPANDED_WIDTH : widgetWidth;
  const targetHeight = isExpanded ? EXPANDED_HEIGHT : widgetHeight;
  const dimensions = {
    width: isMobile ? window.innerWidth : Math.min(targetWidth, 600),
    height: isMobile ? window.innerHeight : Math.min(targetHeight, maxHeight),
  };

  return (
    <div className={`fixed bottom-4 ${positionClass} z-[9999]`}>
      {/* FAB + Teaser */}
      <AnimatePresence>
        {!isOpen && (
          <motion.div
            initial={{ scale: 0, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0, opacity: 0 }}
            className="flex flex-col items-end gap-2"
          >
            {/* Teaser */}
            {showTeaser && (
              <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                className="relative max-w-[220px] rounded-xl bg-card p-3 shadow-lg border"
              >
                <button
                  onClick={() => setShowTeaser(false)}
                  className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-background text-[10px] text-muted-foreground shadow-sm border hover:bg-accent"
                >
                  ×
                </button>
                <p className="text-xs font-medium">👋 Hi there!</p>
                <p className="mt-0.5 text-[11px] text-muted-foreground">
                  Need help? Chat with {botName}
                </p>
              </motion.div>
            )}

            {/* FAB */}
            <button
              onClick={() => {
                setIsOpen(true);
                setShowTeaser(false);
              }}
              className="flex h-14 w-14 items-center justify-center rounded-full shadow-lg transition-transform hover:scale-105 active:scale-95"
              style={{
                backgroundColor: brandColor,
                boxShadow: `0 8px 24px -4px rgba(0,0,0,0.2), 0 0 24px -8px ${brandColor}80`,
                border: "3px solid rgba(255,255,255,0.9)",
              }}
            >
              <MessageCircle className="h-6 w-6 text-white" />
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Chat Panel */}
      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ opacity: 0, scale: 0.9, y: 20 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.9, y: 20 }}
            transition={{ type: "spring", damping: 25, stiffness: 300 }}
            className="flex flex-col overflow-hidden rounded-2xl border bg-background shadow-2xl"
            style={{
              width: isMobile ? "100vw" : dimensions.width,
              height: isMinimized ? 56 : isMobile ? "100vh" : dimensions.height,
              position: isMobile ? "fixed" : "relative",
              top: isMobile ? 0 : undefined,
              left: isMobile ? 0 : undefined,
              right: isMobile ? 0 : undefined,
              bottom: isMobile ? 0 : undefined,
              borderRadius: isMobile ? 0 : undefined,
            }}
          >
            {/* Header */}
            <div
              className="flex items-center justify-between px-4 py-3"
              style={{ backgroundColor: brandColor }}
            >
              <div className="flex items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-white/20 text-sm font-bold text-white">
                  {botName[0]}
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-white">{botName}</h3>
                  <div className="flex items-center gap-1">
                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-green-400" />
                    <span className="text-[10px] text-white/80">Online</span>
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-0.5">
                <button
                  onClick={() => setIsMinimized(!isMinimized)}
                  className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                  title={isMinimized ? "Restore" : "Minimize"}
                >
                  {isMinimized ? <Plus className="h-4 w-4" /> : <Minus className="h-4 w-4" />}
                </button>
                {!isMinimized && !isMobile && (
                  <button
                    onClick={() => setIsExpanded(!isExpanded)}
                    className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                    title={isExpanded ? "Shrink" : "Expand"}
                  >
                    <Maximize2 className="h-4 w-4" />
                  </button>
                )}
                <button
                  onClick={() => setIsOpen(false)}
                  className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                  title="Close"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>

            {/* Chat Body */}
            {!isMinimized && (
              <ChatInterface greeting={greeting} brandColor={brandColor} botName={botName} />
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
