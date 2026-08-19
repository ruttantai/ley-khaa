import { useEffect, useState } from "react";
import Candidates from "./Candidates";
import { fetchCandidates, fetchTasks, type Candidate, type Task } from "./api";

// promoted and abandoned candidates are done: a promoted one is already listed
// below as a Task, so leaving them under "Forming" grew a permanent pile.
const TERMINAL_STATES = ["promoted", "abandoned"];

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [error, setError] = useState<string | null>(null);

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

      <h2 className="text-lg font-semibold mb-2">Forming</h2>
      <Candidates items={candidates.filter((c) => !TERMINAL_STATES.includes(c.state))} />

      <h2 className="text-lg font-semibold mb-2 mt-8">Tasks</h2>
      <ul className="space-y-2">
        {tasks.map((t) => (
          <li key={t.id} className="rounded border border-gray-200 p-3 flex justify-between">
            <span>{t.title}</span>
            <span className="text-sm text-gray-500">
              {t.project} · {t.state}
            </span>
          </li>
        ))}
      </ul>
    </main>
  );
}
