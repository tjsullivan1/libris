import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./settings.js", () => ({
  readSettings: async () => ({ baseUrl: "http://127.0.0.1:8765", credential: "t" }),
  hasPermissionFor: async () => true,
}));

const { createBook, DaemonError, Problem } = await import("./client.js");

function answer(status, body) {
  return { status, ok: status >= 200 && status < 300, json: async () => body };
}

describe("a failed add", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows why when the Shelf could not be fully read", async () => {
    // Given a daemon that cannot tell whether it already holds the book (#168)
    const detail =
      "1 note(s) on the Shelf could not be read, so whether the Library already " +
      "holds this book is not known: Dune - Frank Herbert.md. Nothing was added.";
    fetch.mockResolvedValue(answer(503, { detail }));

    // When the book is added
    const added = createBook({ title: "Dune", authors: ["Frank Herbert"] });

    // Then the person is told that, not that the daemon isn't running
    await expect(added).rejects.toBeInstanceOf(DaemonError);
    await expect(added).rejects.toMatchObject({ problem: Problem.REFUSED, detail });
    await expect(added).rejects.toThrow("could not be read");
  });

  it("shows why when another note already has the book's name", async () => {
    // Given a daemon whose Shelf has a different book under this one's name (#169)
    const detail =
      "A note named Dune - Frank Herbert.md is already on the Shelf, and a new " +
      "note never replaces one. Rename one of the two if they are different " +
      "books. Nothing was added.";
    fetch.mockResolvedValue(answer(409, { detail }));

    // When the book is added
    const added = createBook({ title: "Dune", authors: ["Frank Herbert"] });

    // Then the person is told that, not that the daemon isn't running
    await expect(added).rejects.toMatchObject({ problem: Problem.REFUSED, detail });
    await expect(added).rejects.toThrow("already on the Shelf");
  });

  it("shows why when a new note may have been left partly written", async () => {
    // Given a daemon whose write failed and could not be undone (#193 review)
    const detail =
      "Dune - Frank Herbert.md failed partway through being written (No space " +
      "left on device) and could not be removed, so a partial note may be left " +
      "at that name. Delete it before adding the book again.";
    fetch.mockResolvedValue(answer(500, { detail }));

    // When the book is added
    const added = createBook({ title: "Dune", authors: ["Frank Herbert"] });

    // Then the person is told which note to look at, not to start the server
    await expect(added).rejects.toMatchObject({ problem: Problem.REFUSED, detail });
  });

  it("still reads a 500 without a detail as unreachable", async () => {
    // Given a daemon that failed without saying why
    fetch.mockResolvedValue({
      status: 500,
      ok: false,
      json: async () => {
        throw new SyntaxError("not JSON");
      },
    });

    // When the book is added
    // Then nothing more specific is invented
    await expect(
      createBook({ title: "Dune", authors: ["Frank Herbert"] }),
    ).rejects.toMatchObject({ problem: Problem.UNREACHABLE });
  });

  it("still reads a 404 as something that is not Libris", async () => {
    // Given a base URL that answers, but is not the daemon
    fetch.mockResolvedValue(answer(404, {}));

    // When the book is added
    // Then it is reported as unreachable, as before
    await expect(
      createBook({ title: "Dune", authors: ["Frank Herbert"] }),
    ).rejects.toMatchObject({ problem: Problem.UNREACHABLE });
  });
});
