import { useCallback, useEffect, useState } from "react";
import { fetchDeadLetters, type DeadLetter } from "./api";

// A dropped message with no visible trace is the failure dead letters exist to
// prevent (spec §3.8), so this panel is loud when there is something and
// absent when there is not — an empty "Dead letters" heading on a dashboard
// that is usually healthy is a permanent scar people learn to ignore.
export default function DeadLetters() {
  const [rows, setRows] = useState<DeadLetter[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      fetchDeadLetters()
        .then((r) => {
          setRows(r);
          setError(null);
        })
        .catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  if (error) return <p className="text-red-600 text-sm">{error}</p>;
  if (rows.length === 0) return null;

  return (
    <section>
      <h2 className="text-lg font-semibold mb-2 mt-8 text-red-700">
        {/* fetchDeadLetters caps at 50, so a real flood would otherwise read a
            reassuring "Dead letters (50)". Say "50+" when the page is full. */}
        Dead letters ({rows.length >= 50 ? "50+" : rows.length})
      </h2>
      <ul className="space-y-2">
        {rows.map((row) => (
          <li key={row.id} className="rounded border border-red-200 bg-red-50 p-3">
            <div className="flex items-baseline justify-between">
              <span className="font-medium">{row.reason}</span>
              <span className="text-xs text-gray-500">
                {row.source} · {row.kind}
              </span>
            </div>
            <p className="mt-1 text-xs text-gray-600">
              {new Date(row.created_at).toLocaleString()}
            </p>
            {row.payload && (
              <pre className="mt-2 overflow-x-auto rounded bg-white p-2 text-xs text-gray-700">
                {row.payload}
              </pre>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
