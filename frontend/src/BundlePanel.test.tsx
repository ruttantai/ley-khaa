// frontend/src/BundlePanel.test.tsx
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import BundlePanel from "./BundlePanel";

const bundle = {
  task_id: "t1",
  root: "/work/task-workspaces/task-t1",
  manifest: {
    lane: "synthesis",
    sandbox: "subprocess",
    models: { synthesis: "claude-opus-5" },
    attempts: [
      { attempt: 1, ok: false, reason: "The generated script failed while running." },
      { attempt: 2, ok: true, reason: "Produced output.xlsx in 812 ms.", reasoning: "keyed on ticker" },
    ],
    verdict: { ok: true, reason: "Produced output.xlsx in 812 ms." },
  },
  files: [
    "manifest.json",
    "inputs/bloomberg_universe.csv",
    "generator/attempt_1.py",
    "generator/attempt_2.py",
    "deliverable/output.xlsx",
  ],
  deliverables: ["deliverable/output.xlsx"],
};

const okJson = (body: unknown) => ({ ok: true, json: async () => body });

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url.includes("/bundle/file")
        ? okJson({ path: "generator/attempt_2.py", content: "print('the real script')" })
        : okJson(bundle),
    ),
  );
});
afterEach(cleanup);

test("summarises how the deliverable was produced", async () => {
  render(<BundlePanel taskId="t1" />);
  expect(await screen.findByText(/synthesis/)).toBeTruthy();
  expect(screen.getByText(/subprocess/)).toBeTruthy();
  expect(screen.getByText(/claude-opus-5/)).toBeTruthy();
  expect(screen.getByText(/2 attempts/)).toBeTruthy();
});

test("shows the code that actually ran, on demand", async () => {
  render(<BundlePanel taskId="t1" />);
  fireEvent.click(await screen.findByRole("button", { name: "generator/attempt_2.py" }));
  await waitFor(() => expect(screen.getByText(/the real script/)).toBeTruthy());
});

test("names the sandbox that really ran, not the one we wanted", async () => {
  render(<BundlePanel taskId="t1" />);
  // A panel that says "docker" over a subprocess run would make the bundle
  // overstate its own isolation — the one thing the manifest exists to prevent.
  expect(await screen.findByText(/subprocess/)).toBeTruthy();
  expect(screen.queryByText(/docker/)).toBeNull();
});

test("offers the deliverable and the whole bundle for download", async () => {
  render(<BundlePanel taskId="t1" />);
  const deliverable = (await screen.findByRole("link", { name: /deliverable/i })) as HTMLAnchorElement;
  expect(deliverable.href).toContain("/tasks/t1/bundle/deliverable");
  const whole = screen.getByRole("link", { name: /bundle/i }) as HTMLAnchorElement;
  expect(whole.href).toContain("/tasks/t1/bundle/download");
});

test("says nothing loudly when there is no bundle", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 404 })));
  render(<BundlePanel taskId="t1" />);
  expect(await screen.findByText(/no bundle/i)).toBeTruthy();
});
