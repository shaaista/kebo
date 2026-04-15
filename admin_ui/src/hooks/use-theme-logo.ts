import { useState, useEffect } from "react";
import nexoriaLogoLight from "@/assets/nexoria-logo.png";
import nexoriaLogoDark from "@/assets/nexoria-logo-dark.jpg";

export const useThemeLogo = (): string => {
  const [isDark, setIsDark] = useState(() =>
    typeof window !== "undefined" && document.documentElement.classList.contains("dark")
  );

  useEffect(() => {
    const observer = new MutationObserver(() => {
      setIsDark(document.documentElement.classList.contains("dark"));
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  return isDark ? nexoriaLogoDark : nexoriaLogoLight;
};
