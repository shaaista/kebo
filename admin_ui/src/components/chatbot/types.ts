export type MessageRole = "user" | "bot";

export type IntentType =
  | "booking"
  | "service"
  | "event"
  | "complaint"
  | "callback"
  | "lead"
  | "demo"
  | "faq"
  | "human"
  | "greeting"
  | "feedback"
  | null;

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: Date;
  dataKey?: string;
  quickReplies?: string[];
  formFields?: FormField[];
  attachments?: string[];
  images?: ImageOption[];
  submittedFormData?: Record<string, any>;
}

export interface ImageOption {
  id: string;
  title: string;
  description: string;
  image: string;
  price?: string;
  capacity?: string;
}

export interface FormField {
  name: string;
  label: string;
  type: "text" | "email" | "tel" | "date" | "number" | "select" | "textarea" | "rating" | "tags" | "otp";
  placeholder?: string;
  required?: boolean;
  options?: string[];
}

export interface FeedbackRating {
  emoji: string;
  label: string;
  value: number;
}

export const FEEDBACK_RATINGS: FeedbackRating[] = [
  { emoji: "😞", label: "Bad", value: 1 },
  { emoji: "😐", label: "Okay", value: 2 },
  { emoji: "😊", label: "Good", value: 3 },
  { emoji: "😃", label: "Great", value: 4 },
  { emoji: "🤩", label: "Amazing", value: 5 },
];

export const FEEDBACK_TAGS = [
  "Helpful",
  "Quick Response",
  "Resolved Quickly",
  "Friendly",
  "Professional",
];
