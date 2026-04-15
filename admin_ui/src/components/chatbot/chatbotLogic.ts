import { IntentType, FormField, ImageOption } from "./types";
import { EVENT_TYPES, SERVICE_TYPES, VENUE_TYPES } from "./constants";

interface BotResponse {
  message: string;
  quickReplies?: string[];
  formFields?: FormField[];
  images?: ImageOption[];
  dataKey?: string;
  completed?: boolean;
  resetFields?: string[];
}

export function detectIntent(userInput: string): IntentType {
  const input = userInput.toLowerCase();

  if (/^(hi|hello|hey|namaste|good morning|good evening)/i.test(input)) return "greeting";
  if (/feedback|rate|review|experience|how was|share.thoughts/i.test(input)) return "feedback";
  if (/demo|pricing|contact sales|interested|product info/i.test(input)) return "demo";
  if (/book|reservation|schedule|appointment/i.test(input)) return "booking";
  if (/wedding|event|conference|meeting|banquet|venue/i.test(input)) return "event";
  if (/complaint|issue|problem|not working|help|support/i.test(input)) return "complaint";
  if (/call me|callback|call back|phone me/i.test(input)) return "callback";
  if (/human|agent|person|talk to someone|real person/i.test(input)) return "human";
  if (/service|plan|feature|what do you offer/i.test(input)) return "service";
  if (/\?|what|when|where|how|can you|do you|is there/i.test(input)) return "faq";

  return null;
}

export function getNextBotMessage(
  intent: IntentType,
  _currentIntent: IntentType,
  collectedData: Record<string, any>,
  userInput: string
): BotResponse {

  // ── Demo / Lead Flow ──
  if (intent === "demo") {
    if (!collectedData.name) {
      return { message: "I'd love to help you explore Kebo Bot! What's your name?", dataKey: "name" };
    }
    if (!collectedData.companyName) {
      return { message: `Hi ${collectedData.name}! 👋 What's your company or business name?`, dataKey: "companyName" };
    }
    if (!collectedData.companySize) {
      return {
        message: `How large is ${collectedData.companyName}?`,
        quickReplies: ["1-10 employees", "11-50", "51-200", "200+"],
        dataKey: "companySize",
      };
    }
    if (!collectedData.interest) {
      return {
        message: "What are you most interested in?",
        quickReplies: ["AI Chatbot", "Customer Feedback", "Lead Generation", "Full Suite"],
        dataKey: "interest",
      };
    }
    if (!collectedData.phone) {
      return {
        message: "What's the best number to reach you?",
        formFields: [{ name: "phone", label: "Mobile Number", type: "tel", placeholder: "Enter your mobile number", required: true }],
        dataKey: "phone",
      };
    }
    if (collectedData.phone && !collectedData.otpVerified) {
      if (!collectedData.otp) {
        return {
          message: `We've sent a 4-digit OTP to ${collectedData.phone} for verification. 📱`,
          formFields: [{ name: "otp", label: "Enter OTP", type: "otp", placeholder: "Enter 4-digit OTP", required: true }],
          dataKey: "otp",
        };
      }
    }
    if (!collectedData.email) {
      return { message: "And your email for the demo invite?", dataKey: "email" };
    }
    return {
      message: `Perfect ${collectedData.name}! Our team will reach out to schedule your personalized demo for ${collectedData.companyName}. 🎯`,
      completed: true,
    };
  }

  // ── Booking / Appointment Flow ──
  if (intent === "booking") {
    if (!collectedData.name) {
      return { message: "I'd be happy to help you book an appointment! What's your name?", dataKey: "name" };
    }
    if (!collectedData.serviceType) {
      return {
        message: `Hi ${collectedData.name}! 👋 What type of service are you interested in?`,
        images: SERVICE_TYPES,
        dataKey: "serviceType",
      };
    }
    if (!collectedData.preferredDate) {
      return {
        message: "When would you like to schedule?",
        formFields: [{ name: "preferredDate", label: "Preferred Date", type: "date", required: true }],
        dataKey: "preferredDate",
      };
    }
    if (!collectedData.phone) {
      return {
        message: "What's the best number to reach you?",
        formFields: [{ name: "phone", label: "Mobile Number", type: "tel", placeholder: "Enter your mobile number", required: true }],
        dataKey: "phone",
      };
    }
    if (collectedData.phone && !collectedData.otpVerified) {
      if (!collectedData.otp) {
        return {
          message: `We've sent a 4-digit OTP to ${collectedData.phone} for verification. 📱`,
          formFields: [{ name: "otp", label: "Enter OTP", type: "otp", placeholder: "Enter 4-digit OTP", required: true }],
          dataKey: "otp",
        };
      }
    }
    if (!collectedData.email) {
      return { message: "And your email for the confirmation?", dataKey: "email" };
    }
    return {
      message: `Your ${collectedData.serviceType} booking is noted, ${collectedData.name}! Our team will confirm shortly. 📅`,
      completed: true,
    };
  }

  // ── Event Flow ──
  if (intent === "event") {
    if (!collectedData.eventType) {
      return {
        message: "How exciting! What type of event are you planning?",
        quickReplies: EVENT_TYPES.slice(0, 5),
        dataKey: "eventType",
      };
    }
    if (!collectedData.numberOfGuests) {
      return {
        message: "Approximately how many guests are you expecting?",
        quickReplies: ["Under 50", "50-200", "200-500", "500+"],
        dataKey: "numberOfGuests",
      };
    }
    if (!collectedData.venueType) {
      return { message: "Choose your preferred venue type:", images: VENUE_TYPES, dataKey: "venueType" };
    }
    if (!collectedData.eventDate) {
      return {
        message: "When is the event?",
        formFields: [{ name: "eventDate", label: "Event Date", type: "date", required: true }],
        dataKey: "eventDate",
      };
    }
    if (!collectedData.name) {
      return { message: "What's your name so our events team can reach you?", dataKey: "name" };
    }
    if (!collectedData.phone) {
      return {
        message: `Thanks ${collectedData.name}! What's your phone number?`,
        formFields: [{ name: "phone", label: "Mobile Number", type: "tel", placeholder: "Enter your mobile number", required: true }],
        dataKey: "phone",
      };
    }
    if (collectedData.phone && !collectedData.otpVerified) {
      if (!collectedData.otp) {
        return {
          message: `We've sent a 4-digit OTP to ${collectedData.phone} for verification. 📱`,
          formFields: [{ name: "otp", label: "Enter OTP", type: "otp", placeholder: "Enter 4-digit OTP", required: true }],
          dataKey: "otp",
        };
      }
    }
    return {
      message: `Your ${collectedData.eventType} inquiry for ${collectedData.venueType} has been received! Our events team will reach out with tailored options. 🎉`,
      completed: true,
    };
  }

  // ── Complaint / Support Flow ──
  if (intent === "complaint") {
    if (!collectedData.issueDescription) {
      return { message: "I'm sorry to hear you're facing an issue. Could you briefly describe what happened?", dataKey: "issueDescription" };
    }
    if (!collectedData.name) {
      return { message: "Thank you for sharing that. What's your name?", dataKey: "name" };
    }
    if (!collectedData.contactPreference) {
      return {
        message: `Thanks ${collectedData.name}. What's the best way to reach you?`,
        quickReplies: ["Phone", "Email", "Both"],
        dataKey: "contactPreference",
      };
    }
    if (collectedData.contactPreference !== "Email" && !collectedData.phone) {
      return {
        message: "What's your phone number?",
        formFields: [{ name: "phone", label: "Mobile Number", type: "tel", placeholder: "Enter your mobile number", required: true }],
        dataKey: "phone",
      };
    }
    if (collectedData.phone && !collectedData.otpVerified) {
      if (!collectedData.otp) {
        return {
          message: `We've sent a 4-digit OTP to ${collectedData.phone} for verification. 📱`,
          formFields: [{ name: "otp", label: "Enter OTP", type: "otp", placeholder: "Enter 4-digit OTP", required: true }],
          dataKey: "otp",
        };
      }
    }
    if (collectedData.contactPreference !== "Phone" && !collectedData.email) {
      return { message: "What's your email address?", dataKey: "email" };
    }
    const ticketId = `TKT-${Date.now().toString().slice(-6)}`;
    return {
      message: `Your ticket ${ticketId} has been created, ${collectedData.name}. Our support team will review and respond promptly. 🎫`,
      completed: true,
    };
  }

  // ── Callback Flow ──
  if (intent === "callback") {
    if (!collectedData.name) {
      return { message: "Sure, I can arrange a callback for you! What's your name?", dataKey: "name" };
    }
    if (!collectedData.phone) {
      return {
        message: `Hi ${collectedData.name}! What's the best number to reach you?`,
        formFields: [{ name: "phone", label: "Mobile Number", type: "tel", placeholder: "Enter your mobile number", required: true }],
        dataKey: "phone",
      };
    }
    if (collectedData.phone && !collectedData.otpVerified) {
      if (!collectedData.otp) {
        return {
          message: `We've sent a 4-digit OTP to ${collectedData.phone} for verification. 📱`,
          formFields: [{ name: "otp", label: "Enter OTP", type: "otp", placeholder: "Enter 4-digit OTP", required: true }],
          dataKey: "otp",
        };
      }
    }
    if (!collectedData.preferredTime) {
      return {
        message: "When would you prefer to receive the call?",
        quickReplies: ["Morning (9AM-12PM)", "Afternoon (12PM-3PM)", "Evening (3PM-6PM)", "Anytime"],
        dataKey: "preferredTime",
      };
    }
    if (!collectedData.agenda) {
      return { message: "Could you briefly tell me what you'd like to discuss?", dataKey: "agenda" };
    }
    return {
      message: `Perfect ${collectedData.name}! We'll call you at ${collectedData.phone} during ${collectedData.preferredTime}. 📞`,
      completed: true,
    };
  }

  // ── Human Escalation ──
  if (intent === "human") {
    return {
      message: "Let me connect you with a team member. Please hold on for a moment... 👤\n\nIf all agents are busy, they'll get back to you shortly.",
      completed: true,
    };
  }

  // ── Feedback Flow ──
  if (intent === "feedback") {
    if (!collectedData.feedbackSubmitted) {
      return {
        message: "We'd love to hear about your experience! Please rate us:",
        formFields: [
          { name: "rating", label: "Rate your experience", type: "rating", required: true },
          { name: "feedbackTags", label: "Quick Feedback", type: "tags", required: false },
          { name: "feedbackComment", label: "Additional Comments", type: "textarea", required: false, placeholder: "Tell us more..." },
        ],
        dataKey: "feedbackSubmitted",
      };
    }
    const ratingText = collectedData.rating >= 4 ? "wonderful" : collectedData.rating >= 3 ? "valuable" : "honest";
    return {
      message: `Thank you so much for your ${ratingText} feedback! 🙏\n\nYour input helps us improve and serve you better.`,
      completed: true,
    };
  }

  // ── Service Info ──
  if (intent === "service") {
    return {
      message: "Here are our available plans:",
      images: SERVICE_TYPES,
      quickReplies: ["Request Demo", "Talk to Human"],
    };
  }

  // ── FAQ ──
  if (intent === "faq") {
    return {
      message: getFAQResponse(userInput),
      quickReplies: ["Ask Another Question", "Talk to Human", "Main Menu"],
    };
  }

  // ── Greeting ──
  if (intent === "greeting") {
    return {
      message: "Hello! 👋 How can I help you today?",
      quickReplies: ["Request Demo", "Book Appointment", "Share Feedback", "Ask a Question"],
    };
  }

  // ── Default fallback ──
  return {
    message: "I didn't quite catch that. Could you please rephrase? 🤔",
    quickReplies: ["Request Demo", "Book Appointment", "Share Feedback", "Talk to Human"],
  };
}

function getFAQResponse(question: string): string {
  const q = question.toLowerCase();

  if (/pricing|cost|price/i.test(q))
    return "Our pricing is customized based on your business size and requirements. Request a demo to get a detailed quote!";
  if (/feature|what.*offer|capability/i.test(q))
    return "Kebo Bot offers AI chatbot, customer feedback, lead generation, analytics, and multi-channel support. Would you like a demo?";
  if (/trial|free/i.test(q))
    return "Yes! We offer a 14-day free trial with full access to all features. No credit card required.";
  if (/support|help/i.test(q))
    return "We provide 24/7 support via chat, email, and phone. Our team is always here to help!";
  if (/integration|integrate|connect/i.test(q))
    return "Kebo Bot integrates with websites, WhatsApp, Slack, and most CRM systems. Need details on a specific integration?";
  if (/cancel|refund/i.test(q))
    return "You can cancel anytime. Refunds are processed within 7-10 business days for annual plans.";

  return "That's a great question! Let me connect you with our team who can provide detailed information. Would you like to schedule a callback?";
}
