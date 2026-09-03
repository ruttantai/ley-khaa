import { useEffect, useState } from "react";
import {
  bundleDownloadUrl, deliverableUrl, fetchBundle, fetchBundleFile, promoteTask,
  type Bundle, type Workflow,
} from "./api";

export default function BundlePanel({
  taskId,
  onPromoted,
}: {
  taskId: string;
  // Optional: only wired when the caller (TaskDetail, via App) needs to
  // know a promotion happened elsewhere, e.g. to refresh a sibling Registry.
  onPromoted?: () => void;
}) {
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [missing, setMissing] = useState(false);
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [source, setSource] = useState<string>("");

  useEffect(() => {
    let live = true;
    fetchBundle(taskId)
      .then((b) => live && setBundle(b))
      .catch(() => live && setMissing(true));
    return () => {
      live = false;
    };
  }, [taskId]);

  if (missing) return <p className="text-sm text-gray-500">No bundle for this task yet.</p>;
  if (!bundle) return <p className="text-sm text-gray-500">Loading the bundle…</p>;

  const manifest = bundle.manifest ?? {};
  const attempts = (manifest.attempts as unknown[]) ?? [];
  const generators = bundle.files.filter((f) => f.startsWith("generator/") && f.endsWith(".py"));
  const workflow = manifest.workflow as { name: string; matched_by: string } | undefined;
  const verdictOk = (manifest.verdict as { ok?: boolean } | undefined)?.ok === true;

  const open = (path: string) => {
    setOpenFile(path);
    fetchBundleFile(taskId, path)
      .then(setSource)
      .catch((e) => setSource(String(e)));
  };

  return (
    <section className="rounded border border-gray-200 p-3 space-y-3">
      <h3 className="font-semibold">Output bundle</h3>

      <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
        <dt className="text-gray-500">lane</dt>
        <dd>{String(manifest.lane ?? "—")}</dd>
        <dt className="text-gray-500">sandbox</dt>
        {/* Reported, never inferred: a bundle must not overstate its isolation. */}
        <dd>{String(manifest.sandbox ?? "—")}</dd>
        <dt className="text-gray-500">model</dt>
        <dd>{String((manifest.models as Record<string, string>)?.synthesis ?? "—")}</dd>
        <dt className="text-gray-500">attempts</dt>
        <dd>{attempts.length} attempts</dd>
      </dl>

      {manifest.lane === "registry" && workflow && (
        // A cached run must be visibly cached — this is the one line that
        // tells a human they got the fast path, not a fresh synthesis.
        <p className="rounded bg-blue-50 px-2 py-1 text-sm text-blue-800">
          Replayed <strong>{workflow.name}</strong> from the registry (matched by{" "}
          {workflow.matched_by}) — no model wrote this script.
        </p>
      )}

      {verdictOk && <PromoteControl taskId={taskId} onPromoted={onPromoted} />}

      {generators.length > 0 && (
        <div className="space-y-1">
          <p className="text-sm text-gray-500">The code that produced this:</p>
          <div className="flex flex-wrap gap-2">
            {generators.map((path) => (
              <button
                key={path}
                aria-label={path}
                onClick={() => open(path)}
                className={`rounded border px-2 py-0.5 text-xs ${
                  openFile === path
                    ? "border-blue-500 bg-blue-50 text-blue-800"
                    : "border-gray-200 text-gray-600"
                }`}
              >
                {path}
              </button>
            ))}
          </div>
          {openFile && (
            <pre className="max-h-72 overflow-auto rounded bg-gray-50 p-2 text-xs">{source}</pre>
          )}
        </div>
      )}

      <div className="flex gap-3 text-sm">
        {bundle.deliverables.length > 0 && (
          <a className="text-blue-700 underline" href={deliverableUrl(taskId)}>
            Download the deliverable
          </a>
        )}
        <a className="text-blue-700 underline" href={bundleDownloadUrl(taskId)}>
          Download the whole bundle
        </a>
      </div>
    </section>
  );
}

// A proven bundle freezes into a permanent workflow under a name the human
// picks. Kept inline in the panel — this app has no modal abstraction — and
// collapsed to a single button until opened, so the common case (browsing a
// bundle) stays uncluttered.
function PromoteControl({
  taskId,
  onPromoted,
}: {
  taskId: string;
  onPromoted?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saved, setSaved] = useState<Workflow | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (saved) {
    return <p className="text-sm text-emerald-700">Promoted as {saved.name}.</p>;
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="rounded border border-gray-300 px-3 py-1 text-sm text-gray-700"
      >
        Promote
      </button>
    );
  }

  return (
    <div className="space-y-2 rounded border border-gray-200 p-2">
      {error && <p className="text-sm text-red-600">{error}</p>}
      <div>
        <label htmlFor={`${taskId}-promote-name`} className="text-sm text-gray-500">
          name
        </label>
        <input
          id={`${taskId}-promote-name`}
          className="w-full rounded border border-gray-200 px-2 py-0.5 text-sm"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={`${taskId}-promote-description`} className="text-sm text-gray-500">
          description (optional)
        </label>
        <input
          id={`${taskId}-promote-description`}
          className="w-full rounded border border-gray-200 px-2 py-0.5 text-sm"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>
      <div className="flex gap-2">
        <button
          onClick={() =>
            promoteTask(taskId, name, description)
              .then((workflow) => {
                setSaved(workflow);
                onPromoted?.();
              })
              .catch((e) => setError(String(e)))
          }
          className="rounded bg-emerald-600 px-3 py-1 text-sm text-white"
        >
          Save
        </button>
        <button
          onClick={() => {
            setOpen(false);
            setError(null);
            // name/description must not survive a cancel — otherwise
            // reopening Promote shows what was typed before the human
            // backed out, not a blank form.
            setName("");
            setDescription("");
          }}
          className="text-sm text-gray-500 underline"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
