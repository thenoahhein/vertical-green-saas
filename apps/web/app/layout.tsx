import type { ReactNode } from "react";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="en"><body>{children}</body></html>;
}
