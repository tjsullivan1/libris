import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The popup talks to the daemon only through the client, so the client is the
// seam: everything above it - the markup, the flow between views - is real.
const client = vi.hoisted(() => ({
  health: vi.fn(),
  fields: vi.fn(),
  lookup: vi.fn(),
  findBook: vi.fn(),
  createBook: vi.fn(),
}));

vi.mock("./lib/client.js", () => ({
  ...client,
  DaemonError: class DaemonError extends Error {},
}));

vi.mock("./content/scrapers.js", () => ({
  isBookish: () => true,
  scraperFor: () => () => null,
}));

const SCRAPE = { title: "The Brass Verdict", authors: [] };

// A Google Books volume with no author, as the daemon serves it: the placeholder
// stands where the author would be.
const AUTHORLESS = {
  title: "The Brass Verdict",
  authors: ["Unknown Author"],
  google_books_id: "v1",
};

const NOT_FOUND = { found: false, near_matches: [] };

const el = (id) => document.getElementById(id);

/** Open the popup on a page, as clicking the toolbar button does. */
async function openPopup() {
  // The stylesheet goes: happy-dom would fetch it from a server that isn't there.
  const html = readFileSync(join(import.meta.dirname, "popup.html"), "utf8").replace(
    /<link[^>]*>/g,
    "",
  );
  document.body.innerHTML = new DOMParser().parseFromString(html, "text/html").body.innerHTML;
  globalThis.chrome = {
    tabs: { query: async () => [{ id: 1, url: "https://example.com/book" }] },
    scripting: { executeScript: async () => [{ result: SCRAPE }] },
    runtime: { openOptionsPage: () => {} },
  };
  vi.resetModules();
  await import("./popup.js");
  await vi.waitFor(() => expect(el("candidates").hidden).toBe(false));
}

async function pickTheOnlyCandidate() {
  el("candidate-list").querySelector("button").click();
  await vi.waitFor(() => expect(el("details").hidden).toBe(false));
}

beforeEach(() => {
  vi.clearAllMocks();
  client.health.mockResolvedValue({});
  client.fields.mockResolvedValue({ fields: {} });
  client.lookup.mockResolvedValue({ candidates: [AUTHORLESS] });
  client.createBook.mockResolvedValue({ outcome: "created", book: AUTHORLESS });
});

describe("a candidate Near Matches could not be checked for", () => {
  it("offers to add the author, and checks again under it", async () => {
    // Given a candidate the daemon could not check, because it has no author
    client.findBook.mockResolvedValue({ ...NOT_FOUND, near_match_check: "no_author" });
    await openPopup();
    await pickTheOnlyCandidate();
    expect(el("unchecked").hidden).toBe(false);
    expect(el("unchecked-fix").hidden).toBe(false);

    // When the person supplies the author, and the Library holds a Near Match by them
    client.findBook.mockResolvedValue({
      ...NOT_FOUND,
      near_match_check: "checked",
      near_matches: [{ title: "The Brass Verdict: A Novel", authors: ["Michael Connelly"] }],
    });
    el("unchecked-authors").value = " Michael Connelly ";
    el("unchecked-recheck").click();

    // Then the check runs under that author, and the Near Match is offered
    await vi.waitFor(() => expect(el("near-matches").hidden).toBe(false));
    expect(client.findBook).toHaveBeenLastCalledWith(
      expect.objectContaining({ authors: ["Michael Connelly"] }),
    );

    // And a Book added anyway is filed under that author, not the placeholder,
    // or the next check for it would miss it again
    el("near-match-different").click();
    el("add").click();
    await vi.waitFor(() => expect(client.createBook).toHaveBeenCalled());
    expect(client.createBook.mock.calls[0][0].authors).toEqual(["Michael Connelly"]);
  });

  it("stays put when the author box is empty", async () => {
    // Given the same candidate
    client.findBook.mockResolvedValue({ ...NOT_FOUND, near_match_check: "no_author" });
    await openPopup();
    await pickTheOnlyCandidate();
    const calls = client.findBook.mock.calls.length;

    // When Check again is pressed with nothing typed
    el("unchecked-recheck").click();
    await Promise.resolve();

    // Then nothing is asked of the daemon
    expect(client.findBook.mock.calls.length).toBe(calls);
    expect(el("details").hidden).toBe(false);
  });
});

describe("a candidate Near Matches were checked for", () => {
  it("says nothing about the check and offers no author box", async () => {
    // Given a candidate with an author, and nothing like it in the Library
    client.findBook.mockResolvedValue({ ...NOT_FOUND, near_match_check: "checked" });

    // When it is picked
    await openPopup();
    await pickTheOnlyCandidate();

    // Then an empty answer is a finding, and there is nothing to fix
    expect(el("unchecked").hidden).toBe(true);
  });

  it("does not offer the author box when it was the title that was missing", async () => {
    // Given a candidate the daemon could not check for want of a title
    client.findBook.mockResolvedValue({ ...NOT_FOUND, near_match_check: "no_title" });

    // When it is picked
    await openPopup();
    await pickTheOnlyCandidate();

    // Then the reason is shown, but an author would not help
    expect(el("unchecked").hidden).toBe(false);
    expect(el("unchecked-fix").hidden).toBe(true);
  });
});
