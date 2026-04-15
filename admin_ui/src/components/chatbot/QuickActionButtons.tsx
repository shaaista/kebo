import { Calendar, MessageSquare, Phone, Star, HelpCircle } from "lucide-react";

interface QuickActionButtonsProps {
  brandColor: string;
  onActionClick: (action: string) => void;
  disabled?: boolean;
}

const ACTIONS = [
  { label: "Demo", icon: MessageSquare, action: "Request Demo" },
  { label: "Book", icon: Calendar, action: "Book Appointment" },
  { label: "Callback", icon: Phone, action: "Schedule Callback" },
  { label: "Feedback", icon: Star, action: "Share Feedback" },
  { label: "FAQ", icon: HelpCircle, action: "Ask a Question" },
];

export function QuickActionButtons({ brandColor, onActionClick, disabled }: QuickActionButtonsProps) {
  return (
    <div className="flex gap-1 overflow-x-auto border-t bg-background/50 px-2 py-1.5">
      {ACTIONS.map(({ label, icon: Icon, action }) => (
        <button
          key={action}
          onClick={() => onActionClick(action)}
          disabled={disabled}
          className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition-colors hover:bg-accent disabled:opacity-50"
          style={{ color: disabled ? undefined : brandColor }}
        >
          <Icon className="h-3 w-3" />
          {label}
        </button>
      ))}
    </div>
  );
}
