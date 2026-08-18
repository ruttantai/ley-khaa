import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import App from "./App";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      json: async () => [
        { id: "t1", project: "default", state: "done", title: "compare universes" },
      ],
    })),
  );
});

test("renders tasks from the API", async () => {
  render(<App />);
  await waitFor(() => expect(screen.getByText("compare universes")).toBeTruthy());
  expect(screen.getByText(/done/)).toBeTruthy();
});
