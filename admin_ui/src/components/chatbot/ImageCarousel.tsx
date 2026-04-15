import { ImageOption } from "./types";

interface ImageCarouselProps {
  images: ImageOption[];
  onSelect: (id: string, title: string) => void;
  brandColor: string;
}

export function ImageCarousel({ images, onSelect, brandColor }: ImageCarouselProps) {
  return (
    <div className="flex gap-2 overflow-x-auto pb-2">
      {images.map((img) => (
        <div
          key={img.id}
          className="min-w-[160px] max-w-[180px] shrink-0 overflow-hidden rounded-lg border bg-card shadow-sm"
        >
          <img
            src={img.image}
            alt={img.title}
            className="h-24 w-full object-cover"
            loading="lazy"
          />
          <div className="p-2">
            <h4 className="text-xs font-semibold">{img.title}</h4>
            <p className="mt-0.5 text-[10px] text-muted-foreground line-clamp-2">{img.description}</p>
            {(img.price || img.capacity) && (
              <span className="mt-1 block text-[10px] font-medium" style={{ color: brandColor }}>
                {img.price || img.capacity}
              </span>
            )}
            <button
              onClick={() => onSelect(img.id, img.title)}
              className="mt-1.5 w-full rounded-md py-1 text-[11px] font-medium text-white transition-colors hover:opacity-90"
              style={{ backgroundColor: brandColor }}
            >
              Select
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
