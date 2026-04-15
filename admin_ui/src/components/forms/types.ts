import type { FormField } from "./FormFieldBuilder";

export interface FormConfig {
  id: string;
  name: string;
  triggerId: string;
  triggerCondition: string;
  enabled: boolean;
  fields: FormField[];
}
