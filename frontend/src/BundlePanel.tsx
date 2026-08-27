import { useEffect, useState } from "react";
import { bundleDownloadUrl, deliverableUrl, fetchBundle, fetchBundleFile, type Bundle } from "./api";

export default function BundlePanel({ taskId }: { taskId: string }) {
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
