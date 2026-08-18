import { useEffect, useState } from "react";
import { fetchTasks, type Task } from "./api";

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchTasks().then(setTasks).catch((e) => setError(String(e)));
  }, []);

  return (
    <main className="mx-auto max-w-3xl p-8">
      <h1 className="text-2xl font-bold mb-6">ley-khaa · tasks</h1>
      {error && <p className="text-red-600">{error}</p>}
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
