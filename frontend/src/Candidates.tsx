import type { Candidate } from "./api";

const STATE_STYLES: Record<string, string> = {
  forming: "bg-gray-100 text-gray-700",
  crystallizing: "bg-amber-100 text-amber-800",
  ready: "bg-emerald-100 text-emerald-800",
  promoted: "bg-blue-100 text-blue-800",
  abandoned: "bg-gray-100 text-gray-400",
};

export default function Candidates({ items }: { items: Candidate[] }) {
  if (items.length === 0) {
    return <p className="text-sm text-gray-500">No candidates forming.</p>;
  }
  return (
    <ul className="space-y-2">
      {items.map((c) => (
        <li key={c.id} className="rounded border border-gray-200 p-3">
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium">{c.title}</span>
            <span className={`rounded px-2 py-0.5 text-xs ${STATE_STYLES[c.state] ?? ""}`}>
              {c.state}
            </span>
          </div>
          <p className="mt-1 text-sm text-gray-500">
            {c.message_ids.length} messages · {c.conversation_id}
          </p>
          {c.open_question && (
            <p className="mt-1 text-sm text-amber-700">❓ {c.open_question}</p>
          )}
        </li>
      ))}
    </ul>
  );
}
