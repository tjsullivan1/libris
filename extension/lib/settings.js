// Where the Library is, how it authenticates, and the credential for it.
//
// Three fields rather than two (ADR 0020): the daemon takes a bearer token and
// the Container App takes Entra, so a client that stores only a URL and a
// secret cannot say which of them it holds. Only `bearer` is implemented.
const DEFAULTS = {
  baseUrl: "http://127.0.0.1:8787",
  authMode: "bearer",
  credential: "",
};

export async function readSettings() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULTS));
  return { ...DEFAULTS, ...stored };
}

export async function writeSettings(patch) {
  await chrome.storage.local.set(patch);
}

/**
 * The host permission pattern covering a base URL.
 *
 * The manifest can only name the default daemon, so any other endpoint - a
 * different port, or the remote - is granted at runtime from the options page.
 */
export function originPatternFor(baseUrl) {
  try {
    return `${new URL(baseUrl).origin}/*`;
  } catch {
    return null;
  }
}

export async function hasPermissionFor(baseUrl) {
  const origin = originPatternFor(baseUrl);
  if (!origin) return false;
  return chrome.permissions.contains({ origins: [origin] });
}

/** Must be called from a user gesture; Chrome refuses it otherwise. */
export async function requestPermissionFor(baseUrl) {
  const origin = originPatternFor(baseUrl);
  if (!origin) return false;
  return chrome.permissions.request({ origins: [origin] });
}
