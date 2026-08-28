// What a page yields, as scraped.
//
// Each scraper is injected into the page with `chrome.scripting.executeScript`,
// which serialises the function and runs it there - so a scraper may reference
// globals like `document`, but never anything else in this module. That is why
// the small helpers below are repeated inside each one rather than shared: a
// call out to module scope would arrive in the page as a missing identifier.
//
// The defaults are what makes them testable: injection calls them with no
// arguments and they read the page, while a test passes a document and a URL.

const AMAZON_ASIN = /\/(?:dp|gp\/product)\/([A-Z0-9]{10})(?:[/?#]|$)/;
const GOODREADS_ID = /\/book\/show\/(\d+)/;

/** Amazon: the ASIN is in the URL, and print editions state an ISBN. */
export function scrapeAmazon(doc = document, url = location.href) {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim() || null;
  const textOf = (selector) => clean(doc.querySelector(selector)?.textContent);

  const asin = AMAZON_ASIN.exec(url)?.[1] || null;

  // The detail block is a list of "label : value" rows rather than a table with
  // usable selectors, so the label is what identifies the row.
  let isbn = null;
  for (const row of doc.querySelectorAll("#detailBullets_feature_div li, .rpi-attribute-content")) {
    const text = clean(row.textContent) || "";
    const found = /ISBN-1[03]\s*:?\s*([\d-]{10,17}X?)/i.exec(text);
    if (found) {
      isbn = found[1].replace(/-/g, "");
      // ISBN-13 identifies the edition more precisely, so it wins if both show.
      if (isbn.length === 13) break;
    }
  }

  const authors = [];
  for (const link of doc.querySelectorAll("#bylineInfo .author .a-link-normal, #bylineInfo .contributorNameID")) {
    const name = clean(link.textContent);
    if (name && !authors.includes(name)) authors.push(name);
  }

  return {
    isbn,
    asin,
    title: textOf("#productTitle"),
    authors,
    source_url: url,
  };
}

/** Goodreads: a JSON-LD block when the page has one, the DOM when it does not. */
export function scrapeGoodreads(doc = document, url = location.href) {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim() || null;
  const textOf = (selector) => clean(doc.querySelector(selector)?.textContent);

  let title = null;
  let authors = [];
  let isbn = null;

  for (const block of doc.querySelectorAll('script[type="application/ld+json"]')) {
    let data;
    try {
      data = JSON.parse(block.textContent);
    } catch {
      continue;
    }
    for (const entry of [].concat(data["@graph"] || data)) {
      if (entry?.["@type"] !== "Book") continue;
      title = clean(entry.name) || title;
      isbn = clean(entry.isbn) || isbn;
      authors = [].concat(entry.author || [])
        .map((a) => clean(typeof a === "string" ? a : a?.name))
        .filter(Boolean);
    }
  }

  title = title || textOf('h1[data-testid="bookTitle"]') || textOf("#bookTitle");
  if (!authors.length) {
    for (const node of doc.querySelectorAll('.ContributorLink__name, .authorName span')) {
      const name = clean(node.textContent);
      if (name && !authors.includes(name)) authors.push(name);
    }
  }
  isbn = isbn || clean(doc.querySelector('meta[property="books:isbn"]')?.content);

  return {
    isbn,
    asin: null,
    title,
    authors,
    source_url: url,
    goodreads_id: GOODREADS_ID.exec(url)?.[1] || null,
  };
}

/** Anywhere else: Open Graph and JSON-LD, so an unlisted site still gives something. */
export function scrapeGeneric(doc = document, url = location.href) {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim() || null;
  const metaOf = (property) =>
    clean(doc.querySelector(`meta[property="${property}"], meta[name="${property}"]`)?.content);

  let title = null;
  let authors = [];
  let isbn = null;

  for (const block of doc.querySelectorAll('script[type="application/ld+json"]')) {
    let data;
    try {
      data = JSON.parse(block.textContent);
    } catch {
      continue;
    }
    for (const entry of [].concat(data["@graph"] || data)) {
      if (entry?.["@type"] !== "Book") continue;
      title = clean(entry.name) || title;
      isbn = clean(entry.isbn) || isbn;
      authors = [].concat(entry.author || [])
        .map((a) => clean(typeof a === "string" ? a : a?.name))
        .filter(Boolean);
    }
  }

  title = title || metaOf("og:title") || clean(doc.title);
  isbn = isbn || metaOf("books:isbn") || metaOf("og:book:isbn");
  if (!authors.length) {
    const author = metaOf("book:author") || metaOf("author");
    if (author) authors = [author];
  }

  return { isbn, asin: null, title, authors, source_url: url };
}

/**
 * The scraper for a hostname.
 *
 * Not injected itself, so it may name the others. One scraper runs per page:
 * merging several would blend a confident reading with a guess and leave no way
 * to tell which field came from where.
 */
export function scraperFor(hostname = "") {
  const host = hostname.toLowerCase();
  if (/(^|\.)amazon\./.test(host)) return scrapeAmazon;
  if (/(^|\.)goodreads\.com$/.test(host)) return scrapeGoodreads;
  return scrapeGeneric;
}

/** Whether a scrape found enough to be worth asking Google Books about. */
export function isBookish(scrape) {
  return Boolean(scrape && (scrape.isbn || scrape.asin || scrape.title));
}
