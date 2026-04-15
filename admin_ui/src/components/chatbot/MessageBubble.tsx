import { format } from "date-fns";
import { ChatMessage } from "./types";
import { ChatbotForm } from "./ChatbotForm";
import { ImageCarousel } from "./ImageCarousel";

interface MessageBubbleProps {
  message: ChatMessage;
  brandColor: string;
  onFormSubmit: (messageId: string, formData: Record<string, any>) => void;
  isFormSubmitted: boolean;
  onImageSelect: (id: string, title: string) => void;
}

export function MessageBubble({
  message,
  brandColor,
  onFormSubmit,
  isFormSubmitted,
  onImageSelect,
}: MessageBubbleProps) {
  const isBot = message.role === "bot";

  return (
    <div className={`flex ${isBot ? "justify-start" : "justify-end"} mb-2`}>
      <div
        className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm ${
          isBot
            ? "rounded-bl-md bg-accent text-foreground"
            : "rounded-br-md text-white"
        }`}
        style={!isBot ? { backgroundColor: brandColor } : undefined}
      >
        <p className="whitespace-pre-wrap break-words">{message.content}</p>

        {message.submittedFormData && (
          <div className="mt-2 rounded-md bg-background/50 p-2 text-[11px]">
            {Object.entries(message.submittedFormData).map(([key, value]) => (
              <div key={key} className="flex gap-1">
                <span className="font-medium capitalize">{key}:</span>
                <span>{String(value)}</span>
              </div>
            ))}
          </div>
        )}

        {message.images && message.images.length > 0 && (
          <div className="mt-2">
            <ImageCarousel images={message.images} onSelect={onImageSelect} brandColor={brandColor} />
          </div>
        )}

        {message.formFields && message.formFields.length > 0 && (
          <div className="mt-2">
            <ChatbotForm
              fields={message.formFields}
              onSubmit={(data) => onFormSubmit(message.id, data)}
              brandColor={brandColor}
              isSubmitted={isFormSubmitted}
            />
          </div>
        )}

        <span className={`mt-1 block text-[10px] ${isBot ? "text-muted-foreground" : "text-white/70"}`}>
          {format(new Date(message.timestamp), "h:mm a")}
        </span>
      </div>
    </div>
  );
}
