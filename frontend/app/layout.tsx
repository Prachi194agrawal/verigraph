import type { Metadata } from "next";
import Link from "next/link";
import { AccessPanel } from "@/components/AccessPanel";
import "./globals.css";

export const metadata: Metadata = {
  title: "VeriGraph for Code",
  description: "Code knowledge graph + LLM evaluation dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="top">
          <Link href="/" className="brand">VeriGraph for Code</Link>
          <nav>
            <Link href="/">Repos &amp; builds</Link>
            <a href={`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/docs`} target="_blank" rel="noreferrer">
              API docs
            </a>
          </nav>
        </header>
        <main><AccessPanel />{children}</main>
      </body>
    </html>
  );
}
