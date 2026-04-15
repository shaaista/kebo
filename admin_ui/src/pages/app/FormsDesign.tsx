import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import FormList from "@/components/forms/FormList";
import FormEditor from "@/components/forms/FormEditor";
import type { FormConfig } from "@/components/forms/types";

const defaultForms: FormConfig[] = [
  {
    id: "form-1",
    name: "Contact Form",
    triggerId: "contact-form",
    triggerCondition: "When user wants to reach the team or ask a question",
    enabled: true,
    fields: [
      { id: "f1", label: "Full Name", type: "text", required: true },
      { id: "f2", label: "Email Address", type: "email", required: true },
      { id: "f3", label: "Phone Number", type: "tel", required: false, countryCode: true, selectedCountryCode: "+91" },
      { id: "f4", label: "Message", type: "textarea", required: true },
    ],
  },
  {
    id: "form-2",
    name: "Feedback Form",
    triggerId: "feedback-form",
    triggerCondition: "After chat session ends",
    enabled: true,
    fields: [
      { id: "fb1", label: "How was your experience?", type: "rating", required: true },
      { id: "fb2", label: "What could we improve?", type: "textarea", required: false },
      { id: "fb3", label: "Would you recommend us?", type: "select", required: true },
      { id: "fb4", label: "Email (optional)", type: "email", required: false },
    ],
  },
  {
    id: "form-3",
    name: "Booking Request",
    triggerId: "booking-request",
    triggerCondition: "When user wants to make a reservation or book a room",
    enabled: true,
    fields: [
      { id: "bk1", label: "Guest Name", type: "text", required: true },
      { id: "bk2", label: "Email", type: "email", required: true },
      { id: "bk3", label: "Check-in Date", type: "text", required: true },
      { id: "bk4", label: "Number of Guests", type: "text", required: true },
      { id: "bk5", label: "Special Requests", type: "textarea", required: false },
    ],
  },
];


const FormsDesign = () => {
  const [forms, setForms] = useState<FormConfig[]>(defaultForms);
  const [selectedId, setSelectedId] = useState<string | null>(defaultForms[0].id);

  const selectedForm = forms.find((f) => f.id === selectedId) ?? null;

  const addForm = () => {
    const id = `form-${Date.now()}`;
    const newForm: FormConfig = {
      id,
      name: "New Form",
      triggerId: "new-form",
      triggerCondition: "",
      enabled: true,
      fields: [{ id: `f-${Date.now()}`, label: "Full Name", type: "text", required: true }],
    };
    setForms([...forms, newForm]);
    setSelectedId(id);
  };

  const deleteForm = (id: string) => {
    const next = forms.filter((f) => f.id !== id);
    setForms(next);
    if (selectedId === id) setSelectedId(next[0]?.id ?? null);
  };

  const toggleForm = (id: string, enabled: boolean) => {
    setForms(forms.map((f) => (f.id === id ? { ...f, enabled } : f)));
  };

  const updateForm = (updates: Partial<FormConfig>) => {
    if (!selectedId) return;
    setForms(forms.map((f) => (f.id === selectedId ? { ...f, ...updates } : f)));
  };

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Forms & Feedback</h1>
          <p className="text-muted-foreground">Manage forms the bot presents during conversations</p>
        </div>
        <Button>Save All Forms</Button>
      </div>

      <div className="grid gap-6 lg:grid-cols-[280px_1fr]">
        <FormList
          forms={forms}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onAdd={addForm}
          onDelete={deleteForm}
          onToggle={toggleForm}
        />
        <div>
          {selectedForm ? (
            <FormEditor form={selectedForm} onUpdate={updateForm} />
          ) : (
            <Card>
              <CardContent className="flex min-h-[300px] items-center justify-center text-muted-foreground">
                Select a form or create a new one
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
};

export default FormsDesign;
