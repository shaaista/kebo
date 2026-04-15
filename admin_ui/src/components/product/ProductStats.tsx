const stats = [
  { value: "4", label: "Content Types" },
  { value: "97%", label: "Accuracy" },
  { value: "30+", label: "Languages" },
  { value: "24/7", label: "Availability" },
];

export const ProductStats = () => (
  <section className="border-y bg-card py-8">
    <div className="mx-auto grid max-w-4xl grid-cols-2 gap-8 px-4 md:grid-cols-4">
      {stats.map((s) => (
        <div key={s.label} className="text-center">
          <div className="text-3xl font-bold text-primary">{s.value}</div>
          <div className="mt-1 text-sm text-muted-foreground">{s.label}</div>
        </div>
      ))}
    </div>
  </section>
);
