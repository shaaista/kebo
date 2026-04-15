import { ImageOption } from "./types";

export const EVENT_TYPES = [
  "Wedding",
  "Conference",
  "Corporate Event",
  "Birthday Party",
  "Anniversary",
  "Product Launch",
  "Exhibition",
  "Other",
];

export const SERVICE_TYPES: ImageOption[] = [
  {
    id: "basic",
    title: "Basic Plan",
    description: "Essential features for small businesses",
    image: "https://images.unsplash.com/photo-1460925895917-afdab827c52f?w=400&auto=format&fit=crop",
    price: "Free",
  },
  {
    id: "pro",
    title: "Professional",
    description: "Advanced features with priority support",
    image: "https://images.unsplash.com/photo-1551288049-bebda4e38f71?w=400&auto=format&fit=crop",
    price: "$49/mo",
  },
  {
    id: "enterprise",
    title: "Enterprise",
    description: "Full suite with custom integrations",
    image: "https://images.unsplash.com/photo-1553877522-43269d4ea984?w=400&auto=format&fit=crop",
    price: "Custom",
  },
];

export const VENUE_TYPES: ImageOption[] = [
  {
    id: "banquet",
    title: "Grand Banquet Hall",
    description: "Accommodates 500+ guests with stage and lighting",
    image: "https://images.unsplash.com/photo-1519167758481-83f29da8c2b7?w=400&auto=format&fit=crop",
    capacity: "500+ guests",
  },
  {
    id: "lawn",
    title: "Garden Lawn",
    description: "Open-air venue perfect for weddings",
    image: "https://images.unsplash.com/photo-1464366400600-7168b8af9bc3?w=400&auto=format&fit=crop",
    capacity: "300 guests",
  },
  {
    id: "conference",
    title: "Conference Room",
    description: "Professional space with AV equipment",
    image: "https://images.unsplash.com/photo-1497366216548-37526070297c?w=400&auto=format&fit=crop",
    capacity: "100 guests",
  },
];
