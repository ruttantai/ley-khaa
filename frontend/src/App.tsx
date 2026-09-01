import { useEffect, useState } from "react";
import Candidates from "./Candidates";
import Projects from "./Projects";
import Registry from "./Registry";
import TaskDetail from "./TaskDetail";
import DeadLetters from "./DeadLetters";
import Triage from "./Triage";
import { fetchCandidates, fetchTasks, type Candidate, type Task } from "./api";

// Candidates that are done forming, and so do not belong under "Forming":
// a promoted one is already listed below as a Task, an abandoned one is over,
// and an awaiting_triage one is shown above in the Triage tray. Leaving any of
// them here grew a permanent pile — and for awaiting_triage it also listed the
// same candidate twice on one screen. Mirrors the backend's TERMINAL_STATES
// (ley_khaa/crystallizer/candidate.py).
const TERMINAL_STATES = ["promoted", "abandoned", "awaiting_triage"];

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [openTask, setOpenTask] = useState<string | null>(null);

  useEffect(() => {
    const load = () => {
      fetchTasks().then(setTasks).catch((e) => setError(String(e)));
      fetchCandidates().then(setCandidates).catch((e) => setError(String(e)));
    };
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  return (
    <main className="mx-auto max-w-3xl p-8">
      <h1 className="text-2xl font-bold mb-6">ley-khaa</h1>
      {error && <p className="text-red-600">{error}</p>}

      <h2 className="text-lg font-semibold mb-2">Projects</h2>
      <Projects />

      <Triage />

      <DeadLetters />

      <h2 className="text-lg font-semibold mb-2 mt-8">Forming</h2>
      <Candidates items={candidates.filter((c) => !TERMINAL_STATES.includes(c.state))} />

      <h2 className="text-lg font-semibold mb-2 mt-8">Tasks</h2>
      <ul className="space-y-2">
        {tasks.map((t) => (
          <li key={t.id} className="rounded border border-gray-200 p-3">
            <button
              className="flex w-full justify-between text-left"
              onClick={() => setOpenTask(openTask === t.id ? null : t.id)}
            >
              <span>{t.title}</span>
              <span className="text-sm text-gray-500">
                {t.project} · {t.state}
                {t.effective_mode ? ` · ${t.effective_mode}` : ""}
              </span>
            </button>
            {openTask === t.id && (
              <div className="mt-3">
                <TaskDetail
                  task={t}
                  onChanged={(updated) =>
                    setTasks((prev) => prev.map((x) => (x.id === updated.id ? updated : x)))
                  }
                />
              </div>
            )}
          </li>
        ))}
      </ul>

      <h2 className="text-lg font-semibold mb-2 mt-8">Registry</h2>
      <Registry />
    </main>
  );
}
