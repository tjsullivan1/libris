import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  isBookish,
  scrapeAmazon,
  scrapeGeneric,
  scrapeGoodreads,
  scraperFor,
} from "./scrapers.js";

/**
 * Parse a saved page.
 *
 * These are the only tests that will notice a site changing its markup, which
 * is the one thing about this extension guaranteed to happen.
 */
function fixture(name) {
  // Joined rather than resolved from a file URL: the happy-dom environment
  // replaces the global URL with the browser's, which does not do file paths.
  const path = join(import.meta.dirname, "fixtures", name);
  return new DOMParser().parseFromString(readFileSync(path, "utf8"), "text/html");
}

describe("scraperFor", () => {
  it("picks the scraper for the site, and the generic one otherwise", () => {
    expect(scraperFor("www.amazon.co.uk")).toBe(scrapeAmazon);
    expect(scraperFor("smile.amazon.com")).toBe(scrapeAmazon);
    expect(scraperFor("www.goodreads.com")).toBe(scrapeGoodreads);
    expect(scraperFor("bookshop.org")).toBe(scrapeGeneric);
  });

  it("is not fooled by a hostname that merely contains a site's name", () => {
    // Given a lookalike domain
    // Then it gets the generic scraper, not Amazon's
    expect(scraperFor("notamazon.example.com")).toBe(scrapeGeneric);
    expect(scraperFor("goodreads.com.evil.example")).toBe(scrapeGeneric);
  });
});

describe("scrapeAmazon", () => {
  it("reads the identifiers, title and author from a product page", () => {
    // Given a saved Amazon book page
    const doc = fixture("amazon-book.html");

    // When it is scraped
    const scrape = scrapeAmazon(doc, "https://www.amazon.co.uk/dp/0441013597/ref=sr_1_1");

    // Then the ASIN comes from the URL and the ISBN-13 from the detail block
    expect(scrape.asin).toBe("0441013597");
    expect(scrape.isbn).toBe("9780441013593");
    expect(scrape.title).toBe("Dune: The Inspiration for the Blockbuster Film");
    expect(scrape.authors).toEqual(["Frank Herbert"]);
    expect(scrape.source_url).toContain("amazon.co.uk");
  });

  it("prefers the ISBN-13 when the page states both", () => {
    // Given a page listing ISBN-10 first
    const scrape = scrapeAmazon(fixture("amazon-book.html"), "https://amazon.com/dp/0441013597");

    // Then the more precise identifier wins
    expect(scrape.isbn).toBe("9780441013593");
  });

  it("reads the ASIN from a /gp/product/ URL too", () => {
    const scrape = scrapeAmazon(
      fixture("amazon-book.html"),
      "https://www.amazon.com/gp/product/B000AQ3GPU?psc=1",
    );
    expect(scrape.asin).toBe("B000AQ3GPU");
  });

  it("returns no ISBN for a Kindle edition rather than inventing one", () => {
    // Given a Kindle page, whose ASIN is not an ISBN-10 and whose detail block
    // states no ISBN at all
    const scrape = scrapeAmazon(
      fixture("amazon-kindle.html"),
      "https://www.amazon.com/dp/B00B7NPRY8",
    );

    // Then the ASIN travels and the ISBN stays empty, so the daemon decides
    // whether it is usable as one rather than being told it is
    expect(scrape.asin).toBe("B00B7NPRY8");
    expect(scrape.isbn).toBeNull();
    expect(scrape.title).toBe("Dune");
  });
});

describe("scrapeGoodreads", () => {
  it("reads the JSON-LD block when the page has one", () => {
    const scrape = scrapeGoodreads(
      fixture("goodreads-book.html"),
      "https://www.goodreads.com/book/show/44767458-dune",
    );

    expect(scrape.title).toBe("Dune (Dune, #1)");
    expect(scrape.isbn).toBe("9780441013593");
    expect(scrape.authors).toEqual(["Frank Herbert"]);
    expect(scrape.goodreads_id).toBe("44767458");
  });

  it("falls back to the DOM when there is no JSON-LD", () => {
    // Given an older layout
    const scrape = scrapeGoodreads(
      fixture("goodreads-no-jsonld.html"),
      "https://www.goodreads.com/book/show/234225.Dune",
    );

    // Then the title is still found and its whitespace normalised
    expect(scrape.title).toBe("Dune");
    expect(scrape.authors).toEqual(["Frank Herbert"]);
    expect(scrape.isbn).toBe("9780441013593");
  });
});

describe("scrapeGeneric", () => {
  it("reads Open Graph metadata from a site nobody wrote a scraper for", () => {
    // Given a bookshop page with no site-specific scraper
    const doc = fixture("generic-book.html");

    // When the generic scraper runs
    const scrape = scrapeGeneric(doc, "https://bookshop.org/p/books/piranesi/1234");

    // Then an unlisted site still yields enough to look the book up
    expect(scrape.title).toBe("Piranesi");
    expect(scrape.isbn).toBe("9781526622426");
    expect(scrape.authors).toEqual(["Susanna Clarke"]);
  });

  it("falls back to the document title when there is no metadata", () => {
    const scrape = scrapeGeneric(fixture("not-a-book.html"), "https://example.com/basket");
    expect(scrape.title).toBe("Shopping Basket");
    expect(scrape.isbn).toBeNull();
    expect(scrape.authors).toEqual([]);
  });
});

describe("isBookish", () => {
  it("passes a scrape carrying any identifier or a title", () => {
    expect(isBookish({ isbn: "9780441013593" })).toBe(true);
    expect(isBookish({ asin: "B00B7NPRY8" })).toBe(true);
    expect(isBookish({ title: "Dune" })).toBe(true);
  });

  it("fails a scrape that found nothing, so no lookup is attempted", () => {
    // The popup says "this doesn't look like a book page" here, which is a
    // different message from "Google Books found nothing" and sends someone
    // looking for a different problem.
    expect(isBookish({ isbn: null, asin: null, title: null })).toBe(false);
    expect(isBookish(null)).toBe(false);
  });
});

describe("survives injection", () => {
  /**
   * Rebuild a scraper the way Chrome does.
   *
   * `chrome.scripting.executeScript({ func })` stringifies the function and
   * evaluates it in the page, where nothing from this module exists. Importing
   * a scraper directly, as every test above does, cannot notice a reference to
   * module scope - it resolves fine in the test and throws in the browser.
   */
  function asInjected(scraper) {
    return new Function(`return (${scraper.toString()})`)();
  }

  it.each([
    ["scrapeAmazon", scrapeAmazon, "amazon-book.html", "https://www.amazon.com/dp/0441013597"],
    ["scrapeGoodreads", scrapeGoodreads, "goodreads-book.html", "https://www.goodreads.com/book/show/44767458-dune"],
    ["scrapeGeneric", scrapeGeneric, "generic-book.html", "https://bookshop.org/p/books/piranesi/1234"],
  ])("%s reaches nothing outside itself", (_name, scraper, page, url) => {
    // Given the scraper as the page will receive it
    const injected = asInjected(scraper);

    // When it runs with no module around it
    const scrape = injected(fixture(page), url);

    // Then it works, rather than throwing on an identifier left behind
    expect(scrape.title).toBeTruthy();
  });
});
