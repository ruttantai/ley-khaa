import { useCallback, useEffect, useState } from "react";
import { fetchTriage, foldCandidate, separateCandidate, type TriageItem } from "./api";

export default function Triage() {
  const [items, setItems] = useState<TriageItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => fetchTriage().then(setItems).catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    load();
  }, [load]);

  // The tray owns its own heading (rather than App.tsx rendering it
  // unconditionally) so that returning null here truly makes the tray
  // disappear when empty, instead of leaving a permanent empty heading
  // behind — most of the time there is nothing to triage.
  if (items.length === 0 && !error) return null;

  const act = (action: (id: string) => Promise<void>, id: string) =>
    action(id)
      .then(() => {
        setError(null);
        return load();
      })
      .catch((e) => setError(String(e)));

  return (
    <div>
      <h2 className="text-lg font-semibold mb-2 mt-8">Needs a decision</h2>
      {error && <p className="text-red-600 text-sm mb-2">{error}</p>}
      <ul className="space-y-2">
        {items.map((item) => (
          <li key={item.candidate_id} className="rounded border border-amber-300 bg-amber-50 p-3">
            <p className="font-medium">{item.title}</p>
            <p className="mt-1 text-sm text-gray-700">
              Looks like an amendment to <span className="font-medium">{item.amends_task_title}</span>{" "}
              ({Math.round(item.confidence * 100)}% sure) — {item.reason}
            </p>
            <div className="mt-2 flex gap-2">
              <button
                className="rounded bg-amber-600 px-2 py-1 text-sm text-white"
                onClick={() => act(foldCandidate, item.candidate_id)}
              >
                Fold in
              </button>
              <button
                className="rounded border border-gray-300 px-2 py-1 text-sm"
                onClick={() => act(separateCandidate, item.candidate_id)}
              >
                Keep separate
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
