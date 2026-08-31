import { DaemonError, fields, health } from "./lib/client.js";
import { readSettings, requestPermissionFor, writeSettings } from "./lib/settings.js";

const el = (id) => document.getElementById(id);

function say(text, tone = "") {
  const node = el("message");
  node.textContent = text;
  node.className = `message ${tone}`;
  node.hidden = false;
}

async function load() {
  const settings = await readSettings();
  el("base-url").value = settings.baseUrl;
  el("credential").value = settings.credential;
}

async function saveAndTest() {
  const baseUrl = el("base-url").value.trim().replace(/\/+$/, "");
  const credential = el("credential").value.trim();

  try {
    new URL(baseUrl);
  } catch {
    say("That doesn't look like a URL.", "bad");
    return;
  }

  // Requested unconditionally, with nothing awaited before it. Chrome ties a
  // permission request to a user gesture, and an intervening await spends the
  // one this click carried. Asking for an origin that is already granted costs
  // nothing and shows no prompt, so the check it replaces was only a way to
  // lose the gesture before the request that needed it.
  if (!(await requestPermissionFor(baseUrl))) {
    say(`Libris can't reach ${baseUrl} without that permission.`, "bad");
    return;
  }

  await writeSettings({ baseUrl, authMode: "bearer", credential });

  try {
    const reached = await health();
    // Health needs no token, so it proves the daemon is there and nothing else.
    // The token is only tested by a call that requires one.
    await fields();
    say(`Connected to Libris ${reached.version}, serving ${reached.vault_path}.`, "good");
  } catch (error) {
    if (error instanceof DaemonError) {
      say(error.message, "bad");
      return;
    }
    throw error;
  }
}

el("save").addEventListener("click", saveAndTest);
load();
