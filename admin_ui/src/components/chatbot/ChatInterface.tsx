import { useState, useRef, useEffect } from "react";
import { Send, Loader2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { ChatMessage, IntentType } from "./types";
import { MessageBubble } from "./MessageBubble";
import { QuickReplies } from "./QuickReplies";
import { QuickActionButtons } from "./QuickActionButtons";
import { EmojiPicker } from "./EmojiPicker";
import { detectIntent, getNextBotMessage } from "./chatbotLogic";
import { format, isSameDay } from "date-fns";

const DateSeparator = ({ date }: { date: Date }) => (
  <div className="my-3 flex items-center gap-2">
    <div className="h-px flex-1 bg-border" />
    <span className="text-[10px] text-muted-foreground">{format(date, "dd MMM yyyy")}</span>
    <div className="h-px flex-1 bg-border" />
  </div>
);

interface ChatInterfaceProps {
  greeting: string;
  brandColor: string;
  botName: string;
}

export function ChatInterface({ greeting, brandColor, botName }: ChatInterfaceProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: "1", role: "bot", content: greeting, timestamp: new Date() },
    {
      id: "2",
      role: "bot",
      content: "How can I assist you today?",
      timestamp: new Date(),
      quickReplies: ["Request Demo", "Book Appointment", "Share Feedback", "Talk to Human"],
    },
  ]);
  const [input, setInput] = useState("");
  const [currentIntent, setCurrentIntent] = useState<IntentType>(null);
  const [collectedData, setCollectedData] = useState<Record<string, any>>({});
  const collectedDataRef = useRef<Record<string, any>>({});
  const [isProcessing, setIsProcessing] = useState(false);
  const [submittedFormMessageIds, setSubmittedFormMessageIds] = useState<Set<string>>(new Set());
  const messagesContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;
    requestAnimationFrame(() => {
      container.scrollTo({ top: container.scrollHeight, behavior: "smooth" });
    });
  }, [messages]);

  const lastMessage = messages[messages.length - 1];
  const hasActiveForm = !!(lastMessage?.role === "bot" && lastMessage?.formFields?.length);

  const getMessagesWithDateSeparators = () => {
    const result: (ChatMessage | { type: "date-separator"; date: Date })[] = [];
    let lastDate: Date | null = null;
    messages.forEach((message) => {
      const messageDate = new Date(message.timestamp);
      if (!lastDate || !isSameDay(lastDate, messageDate)) {
        result.push({ type: "date-separator", date: messageDate });
        lastDate = messageDate;
      }
      result.push(message);
    });
    return result;
  };

  const handleBotResponse = (userInput: string, formData?: Record<string, any>) => {
    try {
      let updatedCollectedData = { ...collectedDataRef.current };

      // Find the dataKey from the last bot message that prompted this response
      const pendingDataKey = [...messages].reverse().find((msg) => msg.role === "bot" && msg.dataKey)?.dataKey;

      if (formData) {
        if (formData.otp) formData.otpVerified = true;
        updatedCollectedData = { ...updatedCollectedData, ...formData };
        // Also mark the bot message's dataKey as completed (handles multi-field forms like feedback)
        if (pendingDataKey && !(pendingDataKey in updatedCollectedData)) {
          updatedCollectedData[pendingDataKey] = true;
        }
      }

      if (!formData && userInput.trim()) {
        if (pendingDataKey) {
          updatedCollectedData = { ...updatedCollectedData, [pendingDataKey]: userInput.trim() };
        }
      }

      const FLOW_BREAK = /^(main menu|cancel|start over|talk to human|agent|help)$/i;
      let effectiveIntent: IntentType;

      if (currentIntent && !FLOW_BREAK.test(userInput.trim())) {
        effectiveIntent = currentIntent;
      } else {
        const detected = detectIntent(userInput);
        effectiveIntent = detected ?? currentIntent;
        if (detected && detected !== currentIntent) {
          setCurrentIntent(detected);
        }
      }

      const botResponse = getNextBotMessage(effectiveIntent, effectiveIntent, updatedCollectedData, userInput);

      if (botResponse.resetFields?.length) {
        for (const field of botResponse.resetFields) {
          delete updatedCollectedData[field];
        }
      }

      collectedDataRef.current = updatedCollectedData;
      setCollectedData(updatedCollectedData);

      const botMessage: ChatMessage = {
        id: (Date.now() + 1).toString(),
        role: "bot",
        content: botResponse.message,
        timestamp: new Date(),
        dataKey: botResponse.dataKey,
        quickReplies: botResponse.quickReplies,
        formFields: botResponse.formFields,
        images: botResponse.images,
      };

      setMessages((prev) => [...prev, botMessage]);

      if (botResponse.completed) {
        setTimeout(() => {
          setMessages((prev) => [
            ...prev,
            {
              id: Date.now().toString(),
              role: "bot",
              content: "Is there anything else I can help you with?",
              timestamp: new Date(),
              quickReplies: ["Request Demo", "Book Appointment", "Ask FAQ"],
            },
          ]);
          setCurrentIntent(null);
          collectedDataRef.current = {};
          setCollectedData({});
          setIsProcessing(false);
        }, 1000);
      } else {
        setIsProcessing(false);
      }
    } catch (error) {
      console.error("Bot response error:", error);
      setMessages((prev) => [
        ...prev,
        {
          id: Date.now().toString(),
          role: "bot",
          content: "Sorry, something went wrong. Please try again.",
          timestamp: new Date(),
          quickReplies: ["Main Menu", "Talk to Human"],
        },
      ]);
      setIsProcessing(false);
    }
  };

  const handleSend = () => {
    if (hasActiveForm || isProcessing || !input.trim()) return;
    setIsProcessing(true);
    const userMessage: ChatMessage = {
      id: Date.now().toString(),
      role: "user",
      content: input,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setTimeout(() => handleBotResponse(input), 500);
  };

  const handleQuickReply = (reply: string) => {
    if (hasActiveForm || isProcessing) return;
    setIsProcessing(true);
    const userMessage: ChatMessage = {
      id: Date.now().toString(),
      role: "user",
      content: reply,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMessage]);
    setTimeout(() => handleBotResponse(reply), 500);
  };

  const handleFormSubmit = (messageId: string, formData: Record<string, any>) => {
    if (submittedFormMessageIds.has(messageId)) return;
    setSubmittedFormMessageIds((prev) => {
      const next = new Set(prev);
      next.add(messageId);
      return next;
    });
    setIsProcessing(true);
    const userMessage: ChatMessage = {
      id: Date.now().toString(),
      role: "user",
      content: "📋 Form submitted",
      timestamp: new Date(),
      submittedFormData: formData,
    };
    setMessages((prev) => [...prev, userMessage]);
    setTimeout(() => handleBotResponse("", formData), 300);
  };

  const handleImageSelect = (selectedId: string, selectedTitle: string) => {
    setIsProcessing(true);
    const userMessage: ChatMessage = {
      id: Date.now().toString(),
      role: "user",
      content: selectedTitle,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMessage]);
    const dataKey = currentIntent === "booking" ? "serviceType" : "venueType";
    setTimeout(() => handleBotResponse(selectedTitle, { [dataKey]: selectedTitle }), 300);
  };

  const handleEmojiSelect = (emoji: string) => {
    setInput((prev) => prev + emoji);
  };

  const messagesWithSeparators = getMessagesWithDateSeparators();

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Messages */}
      <div ref={messagesContainerRef} className="flex-1 overflow-y-auto px-3 py-2">
        {messagesWithSeparators.map((item, index) => {
          if ("type" in item && item.type === "date-separator") {
            return <DateSeparator key={`date-${index}`} date={item.date} />;
          }
          const message = item as ChatMessage;
          return (
            <div key={message.id}>
              <MessageBubble
                message={message}
                brandColor={brandColor}
                onFormSubmit={handleFormSubmit}
                isFormSubmitted={submittedFormMessageIds.has(message.id)}
                onImageSelect={handleImageSelect}
              />
              {message.quickReplies && (
                <QuickReplies replies={message.quickReplies} onSelect={handleQuickReply} brandColor={brandColor} />
              )}
            </div>
          );
        })}
        {isProcessing && (
          <div className="mb-2 flex justify-start">
            <div className="flex items-center gap-2 rounded-2xl rounded-bl-md bg-accent px-3 py-2">
              <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
              <span className="text-xs text-muted-foreground">Typing...</span>
            </div>
          </div>
        )}
      </div>

      {/* Quick Actions */}
      <QuickActionButtons
        brandColor={brandColor}
        onActionClick={handleQuickReply}
        disabled={hasActiveForm || isProcessing}
      />

      {/* Input */}
      <div className="flex items-center gap-1.5 border-t bg-background p-2">
        <EmojiPicker
          onEmojiSelect={handleEmojiSelect}
          disabled={hasActiveForm || isProcessing}
          brandColor={brandColor}
        />
        <Input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSend()}
          placeholder={
            hasActiveForm
              ? "Please complete the form above…"
              : isProcessing
              ? "Please wait…"
              : "Type your message"
          }
          disabled={hasActiveForm || isProcessing}
          className="h-8 flex-1 text-xs"
        />
        <button
          onClick={handleSend}
          disabled={hasActiveForm || isProcessing || !input.trim()}
          className="flex h-8 w-8 items-center justify-center rounded-full text-white transition-colors disabled:opacity-40"
          style={{ backgroundColor: brandColor }}
        >
          <Send className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Powered by */}
      <div className="border-t py-1 text-center text-[9px] text-muted-foreground">
        Powered by {botName}
      </div>
    </div>
  );
}
