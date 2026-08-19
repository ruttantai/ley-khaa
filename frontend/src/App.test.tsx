import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import App from "./App";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => ({
      ok: true,
      json: async () =>
        String(url).includes("/candidates")
          ? []
          : [{ id: "t1", project: "default", state: "done", title: "compare universes" }],
    })),
  );
});

test("renders tasks from the API", async () => {
  render(<App />);
  await waitFor(() => expect(screen.getByText("compare universes")).toBeTruthy());
  expect(screen.getByText(/done/)).toBeTruthy();
});
