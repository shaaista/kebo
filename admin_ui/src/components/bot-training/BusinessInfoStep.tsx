import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

interface BusinessInfo {
  businessName: string;
  city: string;
  botName: string;
  industryType: string;
  currency: string;
  timezone: string;
  language: string;
  timestampFormat: string;
  location: string;
  contactEmail: string;
  contactPhone: string;
  website: string;
  address: string;
  welcomeMessage: string;
}

interface BusinessInfoStepProps {
  data: BusinessInfo;
  onChange: (field: keyof BusinessInfo, value: string) => void;
  onSave: () => void;
}

const BusinessInfoStep = ({ data, onChange, onSave }: BusinessInfoStepProps) => (
  <Card>
    <CardHeader className="rounded-t-lg bg-primary px-6 py-4">
      <div className="flex items-center justify-between">
        <CardTitle className="text-lg text-primary-foreground">Step 2: Business Information</CardTitle>
        <Button size="sm" variant="secondary" onClick={onSave}>Save</Button>
      </div>
    </CardHeader>
    <CardContent className="space-y-4 p-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <div className="space-y-1.5">
          <Label>Business Name *</Label>
          <Input value={data.businessName} onChange={(e) => onChange("businessName", e.target.value)} placeholder="e.g. Grand Palace Hotel" />
        </div>
        <div className="space-y-1.5">
          <Label>City</Label>
          <Input value={data.city} onChange={(e) => onChange("city", e.target.value)} placeholder="e.g. Mumbai" />
        </div>
        <div className="space-y-1.5">
          <Label>Bot Name *</Label>
          <Input value={data.botName} onChange={(e) => onChange("botName", e.target.value)} placeholder="e.g. Nova" />
        </div>
        <div className="space-y-1.5">
          <Label>Industry Type</Label>
          <Select value={data.industryType} onValueChange={(v) => onChange("industryType", v)}>
            <SelectTrigger><SelectValue placeholder="Select" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="hotel">Hotel</SelectItem>
              <SelectItem value="restaurant">Restaurant</SelectItem>
              <SelectItem value="spa">Spa & Wellness</SelectItem>
              <SelectItem value="healthcare">Hospital</SelectItem>
              <SelectItem value="automobile">Automobile</SelectItem>
              <SelectItem value="retail">Retail</SelectItem>
              <SelectItem value="travel">Travel Agency</SelectItem>
              <SelectItem value="events">Event Management</SelectItem>
              <SelectItem value="banquet">Banquet Hall</SelectItem>
              <SelectItem value="education">Education</SelectItem>
              <SelectItem value="realestate">Real Estate</SelectItem>
              <SelectItem value="custom">Custom</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Currency</Label>
          <Select value={data.currency} onValueChange={(v) => onChange("currency", v)}>
            <SelectTrigger><SelectValue placeholder="Select" /></SelectTrigger>
            <SelectContent>
              {["INR", "USD", "EUR", "GBP", "AED"].map((c) => (
                <SelectItem key={c} value={c}>{c}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Timezone</Label>
          <Select value={data.timezone} onValueChange={(v) => onChange("timezone", v)}>
            <SelectTrigger><SelectValue placeholder="Select" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="Asia/Kolkata">Asia/Kolkata</SelectItem>
              <SelectItem value="America/New_York">America/New_York</SelectItem>
              <SelectItem value="Europe/London">Europe/London</SelectItem>
              <SelectItem value="Asia/Dubai">Asia/Dubai</SelectItem>
              <SelectItem value="Asia/Singapore">Asia/Singapore</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Language</Label>
          <Select value={data.language} onValueChange={(v) => onChange("language", v)}>
            <SelectTrigger><SelectValue placeholder="Select" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="english">English</SelectItem>
              <SelectItem value="hindi">Hindi</SelectItem>
              <SelectItem value="spanish">Spanish</SelectItem>
              <SelectItem value="french">French</SelectItem>
              <SelectItem value="arabic">Arabic</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Timestamp Format</Label>
          <Select value={data.timestampFormat} onValueChange={(v) => onChange("timestampFormat", v)}>
            <SelectTrigger><SelectValue placeholder="Select" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="12h">12-hour</SelectItem>
              <SelectItem value="24h">24-hour</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Location</Label>
          <Input value={data.location} onChange={(e) => onChange("location", e.target.value)} placeholder="e.g. Downtown" />
        </div>
        <div className="space-y-1.5">
          <Label>Contact Email</Label>
          <Input type="email" value={data.contactEmail} onChange={(e) => onChange("contactEmail", e.target.value)} placeholder="info@business.com" />
        </div>
        <div className="space-y-1.5">
          <Label>Contact Phone</Label>
          <Input value={data.contactPhone} onChange={(e) => onChange("contactPhone", e.target.value)} placeholder="+91 9876543210" />
        </div>
        <div className="space-y-1.5">
          <Label>Website</Label>
          <Input value={data.website} onChange={(e) => onChange("website", e.target.value)} placeholder="https://example.com" />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label>Address</Label>
        <Textarea value={data.address} onChange={(e) => onChange("address", e.target.value)} placeholder="Full business address" rows={2} />
      </div>
      <div className="space-y-1.5">
        <Label>Welcome Message</Label>
        <Input value={data.welcomeMessage} onChange={(e) => onChange("welcomeMessage", e.target.value)} placeholder="Hi! Welcome to {business_name}. How can I help you today?" />
      </div>
    </CardContent>
  </Card>
);

export default BusinessInfoStep;
export type { BusinessInfo };
