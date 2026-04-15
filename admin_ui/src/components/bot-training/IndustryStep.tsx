import { Building2, UtensilsCrossed, Sparkles, HeartPulse, Car, ShoppingBag, Plane, Calendar, GraduationCap, Home, Landmark, Settings } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

const industries = [
  { value: "hotel", label: "Hotel", icon: Building2 },
  { value: "restaurant", label: "Restaurant", icon: UtensilsCrossed },
  { value: "spa", label: "Spa & Wellness", icon: Sparkles },
  { value: "healthcare", label: "Hospital", icon: HeartPulse },
  { value: "automobile", label: "Automobile", icon: Car },
  { value: "retail", label: "Retail", icon: ShoppingBag },
  { value: "travel", label: "Travel Agency", icon: Plane },
  { value: "events", label: "Event Management", icon: Calendar },
  { value: "banquet", label: "Banquet Hall", icon: Landmark },
  { value: "education", label: "Education", icon: GraduationCap },
  { value: "realestate", label: "Real Estate", icon: Home },
  { value: "custom", label: "Custom", icon: Settings },
];

interface IndustryStepProps {
  selected: string;
  onSelect: (value: string) => void;
  onSave: () => void;
}

const IndustryStep = ({ selected, onSelect, onSave }: IndustryStepProps) => (
  <Card>
    <CardHeader className="rounded-t-lg bg-primary px-6 py-4">
      <div className="flex items-center justify-between">
        <CardTitle className="text-lg text-primary-foreground">Step 1: Choose Your Industry</CardTitle>
        <Button size="sm" variant="secondary" onClick={onSave}>Save</Button>
      </div>
    </CardHeader>
    <CardContent className="p-6">
      <p className="mb-4 text-sm text-muted-foreground">Select your industry to get recommended capabilities and settings</p>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
        {industries.map((ind) => {
          const Icon = ind.icon;
          return (
            <button
              key={ind.value}
              onClick={() => onSelect(ind.value)}
              className={`flex flex-col items-center gap-2 rounded-lg border p-4 text-center transition-colors ${
                selected === ind.value
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border hover:bg-accent"
              }`}
            >
              <Icon className="h-6 w-6" />
              <span className="text-xs font-medium">{ind.label}</span>
            </button>
          );
        })}
      </div>
    </CardContent>
  </Card>
);

export default IndustryStep;
export { industries };
