import { useCallback, useEffect, useState } from "react";
import { fetchProjects, type Project } from "./api";

export default function Projects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => fetchProjects().then(setProjects).catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  if (error) return <p className="text-red-600 text-sm">{error}</p>;

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {projects.map((p) => (
        <div key={p.name} className="rounded border border-gray-200 p-3">
          <div className="flex items-baseline justify-between">
            <span className="font-medium">{p.display_name || p.name}</span>
            <span className="text-xs text-gray-500">{p.name}</span>
          </div>
          <p className="mt-1 text-sm text-gray-600">
            {p.in_flight
              ? `running ${p.in_flight.slice(0, 8)}…`
              : p.queue_depth === 0
                ? "idle"
                : "waiting"}
            {p.queue_depth > 0 ? ` · ${p.queue_depth} queued` : ""}
          </p>
        </div>
      ))}
    </div>
  );
}
