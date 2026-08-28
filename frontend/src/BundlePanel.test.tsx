// frontend/src/BundlePanel.test.tsx
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, test, vi } from "vitest";
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

// Re-stubs fetch with a bundle whose manifest is shallow-merged with
// `overrides.manifest`, and a POST /promote response controlled separately —
// its own tests need both a success shape and a 409-with-detail shape.
const mockBundle = (
  overrides: { manifest?: Record<string, unknown> } = {},
  promoteResponse: { ok: boolean; status?: number; body: unknown } = {
    ok: true,
    body: { name: "" },
  },
) => {
  const merged = { ...bundle, manifest: { ...bundle.manifest, ...overrides.manifest } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return {
          ok: promoteResponse.ok,
          status: promoteResponse.status ?? (promoteResponse.ok ? 200 : 409),
          json: async () => promoteResponse.body,
        };
      }
      if (url.includes("/bundle/file")) {
        return okJson({ path: "generator/attempt_2.py", content: "print('the real script')" });
      }
      return okJson(merged);
    }),
  );
};

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

it("offers promotion only for a bundle that passed", async () => {
  // A failed run has nothing worth freezing; offering the button would invite a
  // 409 the human cannot act on.
  mockBundle({ manifest: { verdict: { ok: false } } });
  render(<BundlePanel taskId="t1" />);
  // The fixture ships two generator/*.py files, so this asserts the panel
  // rendered normally (not the "no bundle" empty state) rather than picking
  // one of two equally-valid matches.
  expect(await screen.findAllByText(/generator/i)).toHaveLength(2);
  expect(screen.queryByRole("button", { name: /promote/i })).toBeNull();
});

it("promotes a passing bundle under a name the human chooses", async () => {
  mockBundle({ manifest: { verdict: { ok: true } } });
  render(<BundlePanel taskId="t1" />);

  fireEvent.click(await screen.findByRole("button", { name: /promote/i }));
  fireEvent.change(screen.getByLabelText(/name/i), { target: { value: "universe_check" } });
  fireEvent.click(screen.getByRole("button", { name: /save/i }));

  await waitFor(() => {
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls;
    const promoteCall = calls.find(([, init]) => init?.method === "POST");
    expect(promoteCall).toBeTruthy();
    const [url, init] = promoteCall!;
    expect(url).toContain("/tasks/t1/promote");
    expect(JSON.parse(init.body as string)).toEqual({ name: "universe_check", description: "" });
  });
});

it("shows why a promotion was refused", async () => {
  // A duplicate name is the common case, and it is fixable — the human needs to
  // read it, not watch the dialog close on nothing.
  mockBundle(
    { manifest: { verdict: { ok: true } } },
    { ok: false, body: { detail: "a workflow named 'universe_check' already exists" } },
  );
  render(<BundlePanel taskId="t1" />);

  fireEvent.click(await screen.findByRole("button", { name: /promote/i }));
  fireEvent.change(screen.getByLabelText(/name/i), { target: { value: "universe_check" } });
  fireEvent.click(screen.getByRole("button", { name: /save/i }));

  await waitFor(() => expect(screen.getByText(/already exists/)).toBeTruthy());
});

it("says a cached run took the fast path and names the workflow", async () => {
  mockBundle({
    manifest: { lane: "registry", workflow: { name: "set_difference", matched_by: "fingerprint" } },
  });
  render(<BundlePanel taskId="t1" />);
  expect(await screen.findByText(/set_difference/)).toBeTruthy();
  expect(screen.getByText(/no model/i)).toBeTruthy();
});
