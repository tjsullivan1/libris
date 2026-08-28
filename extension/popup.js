import { DaemonError, createBook, fields, findBook, health, lookup } from "./lib/client.js";
import { isBookish, scraperFor } from "./content/scrapers.js";

const el = (id) => document.getElementById(id);
const show = (id, visible = true) => {
  el(id).hidden = !visible;
};

const state = {
  scrape: null,
  candidates: [],
  chosen: null,
  vocabularies: {},
  // A Near Match prompt that reappears for a candidate already settled would be
  // clicked through, and a confirmation people click through stops working.
  dismissedNearMatches: new Set(),
};

function say(text, tone = "") {
  const node = el("message");
  node.textContent = text;
  node.className = `message ${tone}`;
  show("message");
}

function report(error) {
  if (error instanceof DaemonError) {
    say(error.message, "bad");
    return;
  }
  say(`Something went wrong: ${error.message}`, "bad");
  throw error;
}

/** Read the page. `activeTab` grants this only for the tab that was clicked. */
async function scrapeActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab.url) return null;

  const [injected] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: scraperFor(new URL(tab.url).hostname),
  });
  return injected?.result ?? null;
}

function describe(book) {
  const authors = (book.authors || []).join(", ");
  return authors ? `${book.title} — ${authors}` : book.title;
}

function renderCandidates(candidates) {
  const list = el("candidate-list");
  list.replaceChildren();
  for (const candidate of candidates) {
    const item = document.createElement("li");
    item.className = "pick";
    if (candidate.thumbnail) {
      const image = document.createElement("img");
      image.src = candidate.thumbnail;
      image.alt = "";
      item.append(image);
    }
    const text = document.createElement("div");
    const year = (candidate.published_date || "").slice(0, 4);
    text.textContent = `${describe(candidate)}${year ? ` (${year})` : ""}`;
    item.append(text);
    item.addEventListener("click", () => choose(candidate));
    list.append(item);
  }
  show("candidates");
}

/** Build the form from what the Library defines, never from a list kept here. */
function renderFields() {
  const host = el("field-controls");
  host.replaceChildren();

  for (const [name, spec] of Object.entries(state.vocabularies)) {
    if (spec.multi) {
      const group = document.createElement("fieldset");
      const legend = document.createElement("legend");
      legend.textContent = name;
      group.append(legend);
      for (const value of spec.values) {
        const label = document.createElement("label");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = value;
        box.dataset.field = name;
        label.append(box, document.createTextNode(` ${value}`));
        group.append(label);
      }
      host.append(group);
      continue;
    }

    const label = document.createElement("label");
    label.textContent = name;
    const select = document.createElement("select");
    select.dataset.field = name;
    for (const value of ["", ...spec.values]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value || "—";
      select.append(option);
    }
    if (name === "status") select.value = "To Read";
    label.append(select);
    host.append(label);
  }

  // Rating has no vocabulary to serve, so this control is the client's own.
  const rating = document.createElement("label");
  rating.textContent = "rating";
  const ratingSelect = document.createElement("select");
  ratingSelect.dataset.field = "rating";
  for (const value of ["", 1, 2, 3, 4, 5]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value || "—";
    ratingSelect.append(option);
  }
  rating.append(ratingSelect);
  host.append(rating);
}

function collectOverrides() {
  const overrides = {};
  for (const select of el("field-controls").querySelectorAll("select")) {
    if (!select.value) continue;
    overrides[select.dataset.field] =
      select.dataset.field === "rating" ? Number(select.value) : select.value;
  }
  const boxes = el("field-controls").querySelectorAll("input[type=checkbox]:checked");
  for (const box of boxes) {
    (overrides[box.dataset.field] ??= []).push(box.value);
  }
  return overrides;
}

function heldAlready(book) {
  el("held-book").textContent = describe(book);
  show("held");
  show("candidates", false);
  show("details", false);
  show("near-matches", false);
}

function offerNearMatches(notes, proceed) {
  const list = el("near-matches-list");
  list.replaceChildren();
  for (const note of notes) {
    const item = document.createElement("li");
    item.textContent = describe(note);
    list.append(item);
  }
  show("near-matches");
  show("details", false);

  el("near-match-same").onclick = () => {
    show("near-matches", false);
    // Confirming prevents a write and merges nothing: a Near Match is not a
    // Duplicate Candidate. Nothing is opened either, because the device running
    // this may hold no Shelf at all.
    say("Left alone — you already have it.", "good");
  };
  el("near-match-different").onclick = () => {
    state.dismissedNearMatches.add(proceed.key);
    show("near-matches", false);
    proceed.run();
  };
}

async function choose(candidate) {
  state.chosen = candidate;
  show("candidates", false);
  el("chosen").textContent = describe(candidate);

  const key = candidate.google_books_id || `${candidate.title}|${candidate.authors}`;
  const showDetails = () => {
    renderFields();
    show("details");
  };

  try {
    // Checked again with the candidate's own identifiers: they are the best the
    // popup will ever hold, where the scrape was only what a page happened to say.
    const answer = await findBook({
      isbn: candidate.isbn,
      googleBooksId: candidate.google_books_id,
      title: candidate.title,
      authors: candidate.authors,
    });
    if (answer.found) return heldAlready(answer.book);
    if (answer.near_matches.length && !state.dismissedNearMatches.has(key)) {
      return offerNearMatches(answer.near_matches, { key, run: showDetails });
    }
  } catch (error) {
    return report(error);
  }
  showDetails();
}

async function add() {
  el("add").disabled = true;
  try {
    const result = await createBook(state.chosen, collectOverrides());
    show("details", false);
    say(
      result.outcome === "created"
        ? `Added ${describe(result.book)}.`
        : `Already in your Library: ${describe(result.book)}.`,
      "good",
    );
  } catch (error) {
    el("add").disabled = false;
    report(error);
  }
}

function offerRetry() {
  el("retry-title").value = state.scrape.title || "";
  el("retry-authors").value = (state.scrape.authors || []).join(", ");
  show("retry");
}

function editedScrape() {
  const authors = el("retry-authors")
    .value.split(",")
    .map((name) => name.trim())
    .filter(Boolean);
  return { ...state.scrape, title: el("retry-title").value.trim() || null, authors };
}

async function searchAgain() {
  state.scrape = editedScrape();
  show("retry", false);
  await findCandidates();
}

/** Add exactly what the page said, unenriched, for autoenrich to fill later. */
function addBare() {
  const edited = editedScrape();
  if (!edited.title) {
    say("A title is needed to add anything.", "bad");
    return;
  }
  state.chosen = { title: edited.title, authors: edited.authors, isbn: edited.isbn };
  show("retry", false);
  el("chosen").textContent = describe(state.chosen);
  renderFields();
  show("details");
}

async function findCandidates() {
  try {
    const { candidates } = await lookup(state.scrape);
    state.candidates = candidates;
    if (!candidates.length) {
      offerRetry();
      return;
    }
    renderCandidates(candidates);
  } catch (error) {
    report(error);
  }
}

async function init() {
  el("open-options").addEventListener("click", (event) => {
    event.preventDefault();
    chrome.runtime.openOptionsPage();
  });
  el("add").addEventListener("click", add);
  el("retry-search").addEventListener("click", searchAgain);
  el("retry-bare").addEventListener("click", addBare);

  say("Checking…");

  let served;
  try {
    // The scrape needs no daemon, so it runs alongside rather than behind it.
    const [, vocabularies, scrape] = await Promise.all([
      health(),
      fields(),
      scrapeActiveTab(),
    ]);
    served = vocabularies;
    state.scrape = scrape;
  } catch (error) {
    report(error);
    return;
  }
  state.vocabularies = served.fields;

  if (!isBookish(state.scrape)) {
    // Distinct from "no candidates found": blaming Google Books for a checkout
    // page sends someone looking for the wrong problem entirely.
    say("This doesn't look like a book page.");
    return;
  }

  show("message", false);

  // The cheap check runs against the raw scrape while the lookup is in flight,
  // so a book you already own is answered before a picker is ever drawn.
  const early = findBook({
    isbn: state.scrape.isbn,
    title: state.scrape.title,
    authors: state.scrape.authors,
  }).catch(() => null);

  await findCandidates();

  const answer = await early;
  if (answer?.found) heldAlready(answer.book);
}

init();
