// Every call to the daemon. The popup owns this traffic (ADR 0019): a host
// permission bypasses CORS, so no allowlist is involved and the service worker
// carries nothing.
import { hasPermissionFor, readSettings } from "./settings.js";

/**
 * What went wrong, in the terms a person can act on.
 *
 * Three unrelated causes produce an identical `TypeError: Failed to fetch` -
 * the daemon is down, the URL is wrong, or the origin was never granted - and
 * "start the server" is the wrong advice for two of them.
 */
export const Problem = {
  NOT_PERMITTED: "not_permitted",
  UNREACHABLE: "unreachable",
  NO_SHELF: "no_shelf",
  UNAUTHORIZED: "unauthorized",
  UPSTREAM: "upstream",
  REFUSED: "refused",
};

const ADVICE = {
  [Problem.NOT_PERMITTED]: (s) => `Grant access to ${s.baseUrl} on the options page.`,
  [Problem.UNREACHABLE]: () => "Libris isn't answering. Start it with `libris serve`.",
  [Problem.NO_SHELF]: () => "No Shelf is configured. Run `libris config --vault <path>`.",
  [Problem.UNAUTHORIZED]: () => "That token doesn't match. Run `libris serve --show-token`.",
  [Problem.UPSTREAM]: () => "Google Books couldn't be reached. Try again in a moment.",
  [Problem.REFUSED]: (_s, detail) => detail || "Libris refused that value.",
};

export class DaemonError extends Error {
  constructor(problem, settings, detail) {
    super(ADVICE[problem](settings, detail));
    this.problem = problem;
    this.detail = detail;
  }
}

async function call(path, { method = "GET", body, auth = true } = {}) {
  const settings = await readSettings();

  // Checked before fetching, because a missing grant is indistinguishable from
  // a dead server once fetch has thrown.
  if (!(await hasPermissionFor(settings.baseUrl))) {
    throw new DaemonError(Problem.NOT_PERMITTED, settings);
  }

  const headers = {};
  if (auth) headers.Authorization = `Bearer ${settings.credential}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(`${settings.baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new DaemonError(Problem.UNREACHABLE, settings);
  }

  if (response.status === 401) throw new DaemonError(Problem.UNAUTHORIZED, settings);
  if (response.status === 502) throw new DaemonError(Problem.UPSTREAM, settings);
  if (response.status === 422) {
    throw new DaemonError(Problem.REFUSED, settings, await detailOf(response));
  }
  if (!response.ok) {
    // Includes 404. The daemon never answers a miss with one (ADR 0021), so a
    // 404 here means the base URL points at something that is not Libris.
    throw new DaemonError(Problem.UNREACHABLE, settings);
  }

  return response.json();
}

async function detailOf(response) {
  try {
    const body = await response.json();
    return typeof body.detail === "string" ? body.detail : null;
  } catch {
    return null;
  }
}

/** Unauthenticated, so a wrong token is distinguishable from a dead daemon. */
export async function health() {
  const body = await call("/health", { auth: false });
  if (!body.vault_configured) {
    throw new DaemonError(Problem.NO_SHELF, await readSettings());
  }
  return body;
}

export const fields = () => call("/api/v1/fields");

export const lookup = (scrape) =>
  call("/api/v1/lookup", { method: "POST", body: scrape });

export function findBook({ isbn, googleBooksId, title, authors = [] }) {
  const params = new URLSearchParams();
  if (isbn) params.set("isbn", isbn);
  if (googleBooksId) params.set("google_books_id", googleBooksId);
  if (title) params.set("title", title);
  for (const author of authors) params.append("authors", author);
  return call(`/api/v1/books?${params}`);
}

export const createBook = (candidate, overrides) =>
  call("/api/v1/books", { method: "POST", body: { candidate, overrides } });
