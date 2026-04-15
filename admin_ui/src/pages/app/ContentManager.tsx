import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Search, Edit2, ChevronDown, Building2, FileText, Image, Video, File } from "lucide-react";

interface ContentItem {
  id: string;
  name: string;
  type: string;
  url?: string;
  enabled: boolean;
  label?: string;
  size?: string;
  property: string;
}

const properties = [
  "The Gateway Hotel Calicut",
  "Vivanta Goa",
  "Taj Malabar Resort & Spa",
  "The Gateway Hotel Athwalines Surat",
  "Taj Exotica Resort & Spa Goa",
  "Vivanta Trivandrum",
];

const initialPages: ContentItem[] = [
  { id: "p1", name: "Home Page", type: "page", url: "/gateway-calicut/", enabled: true, label: "Main", property: "The Gateway Hotel Calicut" },
  { id: "p2", name: "Rooms & Suites", type: "page", url: "/gateway-calicut/rooms", enabled: true, label: "Rooms", property: "The Gateway Hotel Calicut" },
  { id: "p3", name: "Dining", type: "page", url: "/gateway-calicut/dining", enabled: true, label: "Dining", property: "The Gateway Hotel Calicut" },
  { id: "p4", name: "Contact", type: "page", url: "/gateway-calicut/contact", enabled: true, label: "Info", property: "The Gateway Hotel Calicut" },
  { id: "p5", name: "Home Page", type: "page", url: "/vivanta-goa/", enabled: true, label: "Main", property: "Vivanta Goa" },
  { id: "p6", name: "Spa & Wellness", type: "page", url: "/vivanta-goa/spa", enabled: true, label: "Spa", property: "Vivanta Goa" },
  { id: "p7", name: "Beach Activities", type: "page", url: "/vivanta-goa/activities", enabled: true, property: "Vivanta Goa" },
  { id: "p8", name: "Gallery", type: "page", url: "/vivanta-goa/gallery", enabled: false, property: "Vivanta Goa" },
  { id: "p9", name: "Home Page", type: "page", url: "/taj-malabar/", enabled: true, label: "Main", property: "Taj Malabar Resort & Spa" },
  { id: "p10", name: "Luxury Rooms", type: "page", url: "/taj-malabar/rooms", enabled: true, label: "Rooms", property: "Taj Malabar Resort & Spa" },
  { id: "p11", name: "Events & Weddings", type: "page", url: "/taj-malabar/events", enabled: true, label: "Events", property: "Taj Malabar Resort & Spa" },
  { id: "p12", name: "Home Page", type: "page", url: "/gateway-surat/", enabled: true, label: "Main", property: "The Gateway Hotel Athwalines Surat" },
  { id: "p13", name: "Amenities", type: "page", url: "/gateway-surat/amenities", enabled: false, property: "The Gateway Hotel Athwalines Surat" },
  { id: "p14", name: "Home Page", type: "page", url: "/taj-exotica-goa/", enabled: true, label: "Main", property: "Taj Exotica Resort & Spa Goa" },
  { id: "p15", name: "Pool & Beach", type: "page", url: "/taj-exotica-goa/pool", enabled: true, label: "Facilities", property: "Taj Exotica Resort & Spa Goa" },
  { id: "p16", name: "Fine Dining", type: "page", url: "/taj-exotica-goa/dining", enabled: true, label: "Dining", property: "Taj Exotica Resort & Spa Goa" },
  { id: "p17", name: "Home Page", type: "page", url: "/vivanta-trivandrum/", enabled: true, label: "Main", property: "Vivanta Trivandrum" },
  { id: "p18", name: "Rooms", type: "page", url: "/vivanta-trivandrum/rooms", enabled: true, label: "Rooms", property: "Vivanta Trivandrum" },
];

const initialImages: ContentItem[] = [
  { id: "i1", name: "hero-banner.jpg", type: "image", enabled: true, size: "2.4 MB", label: "Hero", property: "The Gateway Hotel Calicut" },
  { id: "i2", name: "lobby-photo.png", type: "image", enabled: true, size: "1.8 MB", label: "Lobby", property: "The Gateway Hotel Calicut" },
  { id: "i3", name: "beach-view.jpg", type: "image", enabled: true, size: "3.1 MB", label: "Views", property: "Vivanta Goa" },
  { id: "i4", name: "pool-area.jpg", type: "image", enabled: true, size: "2.7 MB", property: "Taj Exotica Resort & Spa Goa" },
  { id: "i5", name: "spa-interior.jpg", type: "image", enabled: false, size: "1.2 MB", property: "Vivanta Goa" },
  { id: "i6", name: "restaurant-interior.jpg", type: "image", enabled: true, size: "2.0 MB", label: "Dining", property: "Taj Malabar Resort & Spa" },
  { id: "i7", name: "suite-deluxe.jpg", type: "image", enabled: true, size: "1.9 MB", label: "Rooms", property: "Vivanta Trivandrum" },
];

const initialVideos: ContentItem[] = [
  { id: "v1", name: "property-tour.mp4", type: "video", enabled: true, size: "45 MB", label: "Tour", property: "The Gateway Hotel Calicut" },
  { id: "v2", name: "goa-promo.mp4", type: "video", enabled: false, size: "120 MB", property: "Vivanta Goa" },
  { id: "v3", name: "spa-experience.mp4", type: "video", enabled: true, size: "32 MB", label: "Spa", property: "Taj Malabar Resort & Spa" },
];

const initialFiles: ContentItem[] = [
  { id: "f1", name: "room-rates-2024.pdf", type: "file", enabled: true, size: "340 KB", label: "Pricing", property: "The Gateway Hotel Calicut" },
  { id: "f2", name: "restaurant-menu.pdf", type: "file", enabled: true, size: "2.1 MB", label: "Dining", property: "Vivanta Goa" },
  { id: "f3", name: "event-brochure.pdf", type: "file", enabled: true, size: "5.4 MB", label: "Events", property: "Taj Malabar Resort & Spa" },
  { id: "f4", name: "tariff-card.pdf", type: "file", enabled: false, size: "120 KB", property: "Taj Exotica Resort & Spa Goa" },
];

const typeIcon = (type: string) => {
  switch (type) {
    case "page": return <FileText className="h-3.5 w-3.5 text-muted-foreground" />;
    case "image": return <Image className="h-3.5 w-3.5 text-muted-foreground" />;
    case "video": return <Video className="h-3.5 w-3.5 text-muted-foreground" />;
    default: return <File className="h-3.5 w-3.5 text-muted-foreground" />;
  }
};

const ContentManager = () => {
  const [pages, setPages] = useState(initialPages);
  const [images, setImages] = useState(initialImages);
  const [videos, setVideos] = useState(initialVideos);
  const [files, setFiles] = useState(initialFiles);
  const [search, setSearch] = useState("");
  const [editingLabel, setEditingLabel] = useState<string | null>(null);
  const [expandedProperties, setExpandedProperties] = useState<string[]>([properties[0]]);

  const allItems = [...pages, ...images, ...videos, ...files];

  const toggleExpanded = (property: string) => {
    setExpandedProperties((prev) =>
      prev.includes(property) ? prev.filter((p) => p !== property) : [...prev, property]
    );
  };

  const toggle = (id: string) => {
    const updater = (items: ContentItem[]) =>
      items.map((item) => (item.id === id ? { ...item, enabled: !item.enabled } : item));
    setPages(updater);
    setImages(updater);
    setVideos(updater);
    setFiles(updater);
  };

  const updateLabel = (id: string, label: string) => {
    const updater = (items: ContentItem[]) =>
      items.map((item) => (item.id === id ? { ...item, label } : item));
    setPages(updater);
    setImages(updater);
    setVideos(updater);
    setFiles(updater);
    setEditingLabel(null);
  };

  const getPropertyItems = (property: string) => {
    const all = [...pages, ...images, ...videos, ...files];
    return all.filter(
      (item) =>
        item.property === property &&
        item.name.toLowerCase().includes(search.toLowerCase())
    );
  };

  const getPropertyStats = (property: string) => {
    const propPages = pages.filter((i) => i.property === property);
    const propImages = images.filter((i) => i.property === property);
    const propVideos = videos.filter((i) => i.property === property);
    const propFiles = files.filter((i) => i.property === property);
    return { pages: propPages.length, images: propImages.length, videos: propVideos.length, files: propFiles.length };
  };

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold">Content Manager</h1>
          <p className="text-muted-foreground">
            Review, label, and enable/disable crawled content before training
          </p>
        </div>
        <Button>Publish to Knowledge Base</Button>
      </div>

      <div className="relative">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search content across all properties..."
          className="pl-10"
        />
      </div>

      <div className="space-y-3">
        {properties.map((property, index) => {
          const stats = getPropertyStats(property);
          const items = getPropertyItems(property);
          const isExpanded = expandedProperties.includes(property);
          const enabledCount = items.filter((i) => i.enabled).length;

          if (search && items.length === 0) return null;

          return (
            <Collapsible key={property} open={isExpanded} onOpenChange={() => toggleExpanded(property)}>
              <Card>
                <CollapsibleTrigger asChild>
                  <CardHeader className="cursor-pointer hover:bg-muted/50 transition-colors py-4">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-3">
                        <Building2 className="h-5 w-5 text-primary" />
                        <div>
                          <CardTitle className="text-base">
                            <span className="text-muted-foreground text-sm font-normal mr-2">Entity {index + 1}</span>
                            {property}
                          </CardTitle>
                          <div className="flex items-center gap-3 mt-1">
                            {stats.pages > 0 && (
                              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                <FileText className="h-3 w-3" /> {stats.pages} pages
                              </span>
                            )}
                            {stats.images > 0 && (
                              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                <Image className="h-3 w-3" /> {stats.images} images
                              </span>
                            )}
                            {stats.videos > 0 && (
                              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                <Video className="h-3 w-3" /> {stats.videos} videos
                              </span>
                            )}
                            {stats.files > 0 && (
                              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                                <File className="h-3 w-3" /> {stats.files} files
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                      <div className="flex items-center gap-3">
                        <Badge variant="outline" className="text-xs">
                          {enabledCount}/{items.length} enabled
                        </Badge>
                        <ChevronDown className={`h-4 w-4 text-muted-foreground transition-transform ${isExpanded ? "rotate-180" : ""}`} />
                      </div>
                    </div>
                  </CardHeader>
                </CollapsibleTrigger>
                <CollapsibleContent>
                  <CardContent className="pt-0">
                    <div className="divide-y rounded-lg border">
                      {items.map((item) => (
                        <div
                          key={item.id}
                          className={`flex items-center gap-3 px-4 py-2.5 transition-colors ${!item.enabled ? "opacity-50" : ""}`}
                        >
                          <Switch
                            checked={item.enabled}
                            onCheckedChange={() => toggle(item.id)}
                          />
                          {typeIcon(item.type)}
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <span className="truncate text-sm font-medium">{item.name}</span>
                              {item.url && (
                                <span className="hidden truncate text-xs text-muted-foreground sm:inline">
                                  {item.url}
                                </span>
                              )}
                            </div>
                            {item.size && <span className="text-xs text-muted-foreground">{item.size}</span>}
                          </div>
                          <div className="flex items-center gap-2">
                            {editingLabel === item.id ? (
                              <Input
                                className="h-7 w-24 text-xs"
                                defaultValue={item.label || ""}
                                autoFocus
                                onBlur={(e) => updateLabel(item.id, e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") {
                                    updateLabel(item.id, (e.target as HTMLInputElement).value);
                                  }
                                }}
                              />
                            ) : (
                              <button
                                onClick={() => setEditingLabel(item.id)}
                                className="flex items-center gap-1"
                              >
                                {item.label ? (
                                  <Badge variant="secondary" className="text-xs">
                                    {item.label}
                                  </Badge>
                                ) : (
                                  <span className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                                    <Edit2 className="h-3 w-3" /> Label
                                  </span>
                                )}
                              </button>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </CardContent>
                </CollapsibleContent>
              </Card>
            </Collapsible>
          );
        })}
      </div>
    </div>
  );
};

export default ContentManager;
