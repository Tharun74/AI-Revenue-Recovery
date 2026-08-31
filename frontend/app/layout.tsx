import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });

export const metadata: Metadata = {
  title: "Revenue Recovery Agent — Razorpay AI Buildathon",
  description:
    "AI-powered failed-payment recovery: detect → diagnose → decide → act → stop. Built for Razorpay AI Buildathon Track 03.",
  keywords: ["Razorpay", "AI", "Revenue Recovery", "Failed Payments", "Hackathon"],
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="antialiased">{children}</body>
    </html>
  );
}
