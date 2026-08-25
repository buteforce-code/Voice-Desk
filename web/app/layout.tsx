import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, Inter, Newsreader, Noto_Sans_Tamil } from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--f-sans",
  display: "swap",
});

const newsreader = Newsreader({
  subsets: ["latin"],
  weight: ["400", "500"],
  style: ["normal", "italic"],
  variable: "--f-serif",
  display: "swap",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--f-mono",
  display: "swap",
});

// The transcript is where the product's whole claim is visible, and on
// Windows the default Tamil fallback is a different weight and size from the
// Latin beside it -- which reads as a rendering bug rather than a language.
const tamil = Noto_Sans_Tamil({
  subsets: ["tamil"],
  weight: ["400", "500"],
  variable: "--f-tamil",
  display: "swap",
});

const TITLE = "Voice Desk — the front desk that never misses a call";
const DESCRIPTION =
  "An appointment line for Indian clinics that answers in Tamil, Hindi and English, books into the real diary, and is stopped from saying anything clinical by code rather than by instructions.";

export const metadata: Metadata = {
  title: TITLE,
  description: DESCRIPTION,
  applicationName: "Voice Desk",
  openGraph: {
    title: TITLE,
    description: DESCRIPTION,
    type: "website",
    locale: "en_IN",
  },
  twitter: { card: "summary_large_image", title: TITLE, description: DESCRIPTION },
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // `viewportFit: cover` plus the safe-area padding in the nav and footer is
  // what keeps the page out from under an iPhone's notch and home indicator.
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fcfbf9" },
    { media: "(prefers-color-scheme: dark)", color: "#0c0e11" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en-IN"
      data-shade="paper"
      className={`${inter.variable} ${newsreader.variable} ${mono.variable} ${tamil.variable}`}
    >
      <body>{children}</body>
    </html>
  );
}
