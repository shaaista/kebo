interface QuickRepliesProps {
  replies: string[];
  onSelect: (reply: string) => void;
  brandColor: string;
}

export function QuickReplies({ replies, onSelect, brandColor }: QuickRepliesProps) {
  return (
    <div className="flex flex-wrap gap-1.5 px-1 pb-2">
      {replies.map((reply) => (
        <button
          key={reply}
          onClick={() => onSelect(reply)}
          className="rounded-full border px-3 py-1.5 text-xs font-medium transition-all hover:scale-105 active:scale-95"
          style={{
            borderColor: brandColor,
            color: brandColor,
            backgroundColor: `${brandColor}10`,
          }}
        >
          {reply}
        </button>
      ))}
    </div>
  );
}
