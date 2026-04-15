import { useState } from "react";
import { Smile } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

const EMOJI_CATEGORIES: Record<string, string[]> = {
  "😊": ["😀", "😃", "😄", "😁", "😆", "😅", "🤣", "😂", "🙂", "😊", "😇", "😍", "🥰", "😘"],
  "👍": ["👍", "👎", "👋", "🤝", "🙏", "💪", "👏", "🤞", "✌️", "🤙", "👌", "✨"],
  "❤️": ["❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "💕", "💖", "💗", "💯", "🔥"],
  "🎉": ["🎉", "🎊", "🎈", "🎁", "🎂", "🏆", "🥇", "⭐", "🌟", "💫", "🚀", "💡"],
};

interface EmojiPickerProps {
  onEmojiSelect: (emoji: string) => void;
  disabled?: boolean;
  brandColor: string;
}

export function EmojiPicker({ onEmojiSelect, disabled, brandColor }: EmojiPickerProps) {
  const [open, setOpen] = useState(false);
  const [activeCategory, setActiveCategory] = useState("😊");

  const categories = Object.keys(EMOJI_CATEGORIES);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          disabled={disabled}
          className="p-1.5 text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
        >
          <Smile className="h-4 w-4" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-64 p-2" side="top" align="start">
        <div className="mb-2 flex gap-1 border-b pb-1">
          {categories.map((cat) => (
            <button
              key={cat}
              onClick={() => setActiveCategory(cat)}
              className="rounded px-2 py-1 text-sm transition-colors"
              style={activeCategory === cat ? { backgroundColor: `${brandColor}20` } : {}}
            >
              {cat}
            </button>
          ))}
        </div>
        <div className="grid grid-cols-7 gap-0.5">
          {EMOJI_CATEGORIES[activeCategory].map((emoji) => (
            <button
              key={emoji}
              onClick={() => {
                onEmojiSelect(emoji);
                setOpen(false);
              }}
              className="rounded p-1.5 text-base transition-colors hover:bg-accent"
            >
              {emoji}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}
