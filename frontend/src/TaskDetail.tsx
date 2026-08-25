import { useState } from "react";
import {
  answerTask, approveTask, patchTaskSpec, rejectTask, setTaskMode,
  type Task,
} from "./api";

const MODES = [
  { value: "suggest", label: "Suggest" },
  { value: "copilot", label: "Co-pilot" },
  { value: "auto", label: "Auto" },
];

// Which spec fields a human can correct in place. The rest are the model's
// reading of the request and are better fixed by answering, not editing.
const EDITABLE = ["operation", "output_format", "recipient", "urgency"] as const;

// Only `recipient` is nullable server-side (TaskSpec, backend/ley_khaa/interpreter/
// spec.py). Sending null for the others 422s — clear them to an empty string
// instead.
const NULLABLE_FIELDS: readonly string[] = ["recipient"];

const pct = (value: number | null) => (value === null ? "—" : `${Math.round(value * 100)}%`);

export default function TaskDetail({
  task,
  onChanged,
}: {
  task: Task;
  onChanged: (task: Task) => void;
}) {
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);

  const run = (work: Promise<Task>) =>
    work.then(onChanged).catch((e) => setError(String(e)));

  const waiting = task.state === "awaiting_approval";
  const blocked = task.state === "needs_clarification";
  // Mirrors the driver's _ACTIONABLE set (backend/ley_khaa/orchestrator/
  // driver.py): approve/reject/override/edit_spec all refuse to touch a task
  // outside these two states, so the controls that trigger them must not be
  // live outside them either.
  const modeEditable = waiting || blocked;

  return (
    <div className="rounded border border-gray-200 p-4 space-y-4">
      {error && <p className="text-red-600 text-sm">{error}</p>}

      <div>
        <p className="text-sm text-gray-500">
          confidence {pct(task.confidence)} · risk {pct(task.risk)}
        </p>
        {task.autonomy_reason && <p className="mt-1">{task.autonomy_reason}</p>}
      </div>

      {modeEditable && (
        <div className="flex gap-2">
          {MODES.map((mode) => (
            <button
              key={mode.value}
              aria-label={mode.label}
              aria-pressed={task.effective_mode === mode.value}
              onClick={() => run(setTaskMode(task.id, mode.value))}
              className={`rounded px-3 py-1 text-sm border ${
                task.effective_mode === mode.value
                  ? "border-blue-500 bg-blue-50 text-blue-800"
                  : "border-gray-200 text-gray-600"
              }`}
            >
              {mode.label}
            </button>
          ))}
          {task.mode_override && (
            <button
              onClick={() => run(setTaskMode(task.id, null))}
              className="text-sm text-gray-500 underline"
            >
              follow the recommendation
            </button>
          )}
        </div>
      )}

      {task.spec && (
        <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
          <dt className="text-gray-500">intent</dt>
          <dd>{task.spec.intent}</dd>
          {EDITABLE.map((field) => (
            <FieldRow key={field} task={task} field={field} onChanged={onChanged} onError={setError} />
          ))}
          {task.spec.missing_fields.length > 0 && (
            <>
              <dt className="text-gray-500">missing</dt>
              <dd className="text-amber-700">{task.spec.missing_fields.join(", ")}</dd>
            </>
          )}
        </dl>
      )}

      {blocked && (
        <div className="space-y-2">
          <p className="text-amber-700">❓ {task.open_question}</p>
          <div className="flex gap-2">
            <input
              className="flex-1 rounded border border-gray-200 px-2 py-1 text-sm"
              placeholder="Type your answer…"
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
            />
            <button
              className="rounded bg-blue-600 px-3 py-1 text-sm text-white"
              onClick={() => run(answerTask(task.id, answer)).then(() => setAnswer(""))}
            >
              Answer
            </button>
          </div>
          {/* A human facing a question they cannot answer still needs a way
              out: needs_clarification -> failed is legal server-side. */}
          <button
            className="rounded border border-gray-300 px-3 py-1 text-sm"
            onClick={() => run(rejectTask(task.id, "rejected from the dashboard"))}
          >
            Reject
          </button>
        </div>
      )}

      {waiting && (
        <div className="flex gap-2">
          <button
            className="rounded bg-emerald-600 px-3 py-1 text-sm text-white"
            onClick={() => run(approveTask(task.id))}
          >
            Approve
          </button>
          <button
            className="rounded border border-gray-300 px-3 py-1 text-sm"
            onClick={() => run(rejectTask(task.id, "rejected from the dashboard"))}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}

function FieldRow({
  task, field, onChanged, onError,
}: {
  task: Task;
  field: string;
  onChanged: (task: Task) => void;
  onError: (message: string) => void;
}) {
  const current = String((task.spec as Record<string, unknown>)[field] ?? "");
  const [value, setValue] = useState(current);
  const editable = task.state === "awaiting_approval" || task.state === "needs_clarification";

  if (!editable) {
    return (
      <>
        <dt className="text-gray-500">{field}</dt>
        <dd>{current || "—"}</dd>
      </>
    );
  }
  return (
    <>
      <dt className="text-gray-500">
        <label htmlFor={`${task.id}-${field}`}>{field}</label>
      </dt>
      <dd>
        <input
          id={`${task.id}-${field}`}
          aria-label={field}
          className="w-full rounded border border-gray-200 px-2 py-0.5"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          // Patch on blur, not on every keystroke: each patch re-scores the task
          // on the server, and doing that per character is both wasteful and
          // visibly jumpy.
          onBlur={() => {
            if (value === current) return;
            // Only `recipient` is nullable server-side. Sending null for a
            // cleared operation/output_format/urgency is rejected with a 422
            // (they're non-nullable TaskSpec fields) — send the empty string
            // for those instead, so a clear is still a valid patch.
            const cleared = NULLABLE_FIELDS.includes(field) ? null : "";
            patchTaskSpec(task.id, { [field]: value || cleared })
              .then(onChanged)
              .catch((e) => onError(String(e)));
          }}
        />
      </dd>
    </>
  );
}
