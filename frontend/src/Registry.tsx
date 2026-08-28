import { useEffect, useState } from "react";
import { deleteWorkflow, fetchWorkflows, unquarantineWorkflow, type Workflow } from "./api";

const ORIGIN_STYLES: Record<string, string> = {
  seed: "bg-gray-100 text-gray-700",
  promoted: "bg-blue-100 text-blue-800",
};

export default function Registry() {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    fetchWorkflows()
      .then((w) => {
        setWorkflows(w);
        setError(null);
        setLoaded(true);
      })
      .catch((e) => {
        setError(String(e));
        setLoaded(true);
      });

  useEffect(() => {
    load();
  }, []);

  if (!loaded) return <p className="text-sm text-gray-500">Loading the registry…</p>;
  if (error) return <p className="text-red-600 text-sm">{error}</p>;
  if (workflows.length === 0) {
    return <p className="text-sm text-gray-500">No workflows cached yet.</p>;
  }

  return (
    <ul className="space-y-2">
      {workflows.map((w) => (
        <WorkflowRow
          key={w.name}
          workflow={w}
          onUnquarantine={() => unquarantineWorkflow(w.name).then(load)}
          onDelete={() => deleteWorkflow(w.name).then(load)}
        />
      ))}
    </ul>
  );
}

// Split out so each row can own its own "really delete?" confirm state
// without the parent tracking which row is mid-confirm.
function WorkflowRow({
  workflow,
  onUnquarantine,
  onDelete,
}: {
  workflow: Workflow;
  onUnquarantine: () => void;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const roles = workflow.inputs.map((i) => i.role).join(", ") || "—";

  return (
    <li className="rounded border border-gray-200 p-3">
      <div className="flex items-center justify-between gap-3">
        <span className="font-medium">{workflow.name}</span>
        <div className="flex items-center gap-2">
          <span className={`rounded px-2 py-0.5 text-xs ${ORIGIN_STYLES[workflow.origin] ?? ""}`}>
            {workflow.origin}
          </span>
          {workflow.quarantined && (
            <span className="rounded bg-red-100 px-2 py-0.5 text-xs text-red-800">quarantined</span>
          )}
        </div>
      </div>
      <p className="mt-1 text-sm text-gray-500">{workflow.description}</p>
      <p className="mt-1 text-sm text-gray-500">
        {roles} · {workflow.output_format} · {workflow.runs_ok} ok / {workflow.runs_failed} failed
        · {workflow.source_sha256.slice(0, 8)}
      </p>
      <div className="mt-2 flex gap-2">
        {workflow.quarantined && (
          <button
            onClick={onUnquarantine}
            className="rounded border border-gray-300 px-3 py-1 text-sm text-gray-700"
          >
            Clear quarantine
          </button>
        )}
        {!confirming ? (
          <button
            onClick={() => setConfirming(true)}
            className="rounded border border-gray-300 px-3 py-1 text-sm text-red-700"
          >
            Delete
          </button>
        ) : (
          <button
            onClick={onDelete}
            className="rounded bg-red-600 px-3 py-1 text-sm text-white"
          >
            Really delete?
          </button>
        )}
      </div>
    </li>
  );
}
