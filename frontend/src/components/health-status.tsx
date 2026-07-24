"use client";

import { Loader2 } from "lucide-react";

import { useHealth } from "@/hooks/use-health";

export function HealthStatus() {
  const { data, isLoading, isError } = useHealth();

  if (isLoading) {
    return (
      <span className="text-muted-foreground inline-flex items-center gap-2 text-sm">
        <Loader2 className="animate-spin" /> Checking API status…
      </span>
    );
  }

  if (isError || !data) {
    return (
      <span className="inline-flex items-center gap-2 text-sm text-red-600 dark:text-red-400">
        <span className="size-2 rounded-full bg-red-600 dark:bg-red-400" />
        API unreachable
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-2 text-sm text-emerald-600 dark:text-emerald-400">
      <span className="size-2 rounded-full bg-emerald-600 dark:bg-emerald-400" />
      API {data.status} · {data.environment}
    </span>
  );
}
