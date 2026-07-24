import { ShieldCheck } from "lucide-react";

import { HealthStatus } from "@/components/health-status";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const CAPABILITIES = [
  "Source code scanning",
  "Secret detection",
  "Dependency analysis",
  "Container scanning",
  "Website scanning",
  "API scanning",
  "Authenticated security testing",
  "Automated penetration testing",
  "AI-powered business logic testing",
  "Continuous monitoring",
  "Security reporting",
];

export default function Home() {
  return (
    <div className="bg-background flex flex-1 flex-col items-center">
      <main className="flex w-full max-w-4xl flex-1 flex-col gap-10 px-6 py-16 sm:px-8">
        <header className="flex flex-col gap-4">
          <div className="flex items-center gap-2">
            <ShieldCheck className="text-primary size-6" />
            <span className="text-lg font-semibold tracking-tight">SentinelX</span>
          </div>
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            Continuous security validation platform
          </h1>
          <p className="text-muted-foreground max-w-2xl text-base leading-relaxed">
            SentinelX is under active development. This is the project scaffold — authentication,
            scanning engines, and reporting are implemented incrementally on top of this foundation.
          </p>
          <div className="flex items-center gap-4">
            <Button>Get started</Button>
            <Button variant="outline">View documentation</Button>
            <HealthStatus />
          </div>
        </header>

        <Card>
          <CardHeader>
            <CardTitle>Platform capabilities</CardTitle>
            <CardDescription>
              Authorized security testing across the full application lifecycle.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="grid grid-cols-1 gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
              {CAPABILITIES.map((capability) => (
                <li key={capability} className="flex items-center gap-2">
                  <span className="bg-primary size-1.5 rounded-full" />
                  {capability}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </main>
    </div>
  );
}
