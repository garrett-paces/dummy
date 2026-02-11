#!/usr/bin/env python3
"""
De Soto Kansas City Code Scraper
=================================
Scrapes all pages from https://desotokansas.citycode.net and consolidates
them into a single PDF document.

The site is a JavaScript SPA using hashbang (#!) routing. Each hashbang
fragment corresponds to a .htm file loaded via AJAX. This scraper uses
Playwright to render each page and then generates a consolidated PDF.

Usage:
    python3 scraper.py [options]

Requirements:
    pip install playwright pypdf
    playwright install chromium
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright, Page, Browser
from pypdf import PdfWriter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://desotokansas.citycode.net"
INDEX_URL = f"{BASE_URL}/index.html"
TOC_HASH = "#!tableOfContents"
DEFAULT_OUTPUT = "desoto_city_code.pdf"
DEFAULT_TEMP_DIR = "temp_pdfs"

# Maximum concurrent page-to-PDF conversions (be polite to the server)
MAX_CONCURRENCY = 3

# Delays / timeouts (milliseconds unless noted)
PAGE_LOAD_TIMEOUT = 60_000
CONTENT_SETTLE_DELAY = 2  # seconds – wait for JS rendering to settle
NAVIGATION_DELAY = 1  # seconds – delay between page navigations
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2  # seconds – base backoff for retries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: Discover all content page links from the Table of Contents
# ---------------------------------------------------------------------------

async def discover_sections(page: Page) -> list[dict]:
    """Navigate to the Table of Contents and extract all section links.

    Returns a list of dicts: [{"title": "...", "hash": "...", "url": "..."}, ...]
    """
    toc_url = f"{INDEX_URL}{TOC_HASH}"
    log.info("Loading Table of Contents: %s", toc_url)
    await page.goto(toc_url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
    await asyncio.sleep(CONTENT_SETTLE_DELAY)

    # The TOC page contains links whose hrefs use the #! pattern.
    # We look for all <a> tags whose href contains "#!"
    links = await page.evaluate("""
        () => {
            const anchors = document.querySelectorAll('a[href*="#!"]');
            const seen = new Set();
            const results = [];
            for (const a of anchors) {
                const href = a.getAttribute('href');
                // Extract the hashbang fragment
                const match = href.match(/#!(.+)/);
                if (match) {
                    const hash = match[1];
                    if (!seen.has(hash)) {
                        seen.add(hash);
                        results.push({
                            title: a.textContent.trim(),
                            hash: hash,
                            url: href
                        });
                    }
                }
            }
            return results;
        }
    """)

    # If the TOC approach yields nothing, try the _list.html page as fallback
    if not links:
        log.warning("No links found on TOC page, trying _list.html fallback...")
        links = await _discover_from_list_page(page)

    # If still nothing, try scraping the sidebar/navigation
    if not links:
        log.warning("No links from _list.html either, trying sidebar navigation...")
        links = await _discover_from_sidebar(page)

    log.info("Discovered %d unique sections", len(links))
    return links


async def _discover_from_list_page(page: Page) -> list[dict]:
    """Fallback: try the _list.html page which some citycode.net sites expose."""
    list_url = f"{BASE_URL}/_list.html"
    log.info("Loading list page: %s", list_url)
    try:
        await page.goto(list_url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
        await asyncio.sleep(CONTENT_SETTLE_DELAY)
    except Exception as e:
        log.warning("Could not load _list.html: %s", e)
        return []

    return await page.evaluate("""
        () => {
            const anchors = document.querySelectorAll('a[href]');
            const seen = new Set();
            const results = [];
            for (const a of anchors) {
                const href = a.getAttribute('href');
                // Look for .htm links or #! links
                let hash = null;
                const hashMatch = href.match(/#!(.+)/);
                const htmMatch = href.match(/^([\\w]+)\\.htm$/);
                if (hashMatch) {
                    hash = hashMatch[1];
                } else if (htmMatch) {
                    hash = htmMatch[1];
                }
                if (hash && !seen.has(hash)) {
                    seen.add(hash);
                    results.push({
                        title: a.textContent.trim(),
                        hash: hash,
                        url: href
                    });
                }
            }
            return results;
        }
    """)


async def _discover_from_sidebar(page: Page) -> list[dict]:
    """Fallback: navigate to main page and scrape the sidebar/nav panel."""
    main_url = f"{INDEX_URL}#!codeOfTheCityOfDeSotoKansas"
    log.info("Loading main page for sidebar: %s", main_url)
    try:
        await page.goto(main_url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
        await asyncio.sleep(CONTENT_SETTLE_DELAY)
    except Exception as e:
        log.warning("Could not load main page: %s", e)
        return []

    # Try to expand all collapsed tree nodes in the sidebar
    await page.evaluate("""
        () => {
            // Click all expander/toggle buttons to reveal nested links
            const expanders = document.querySelectorAll(
                '.tree-toggle, .expand, .collapse, [data-toggle], .fa-plus, .fa-caret-right'
            );
            for (const el of expanders) {
                try { el.click(); } catch(e) {}
            }
        }
    """)
    await asyncio.sleep(CONTENT_SETTLE_DELAY)

    return await page.evaluate("""
        () => {
            const anchors = document.querySelectorAll('a[href*="#!"]');
            const seen = new Set();
            const results = [];
            for (const a of anchors) {
                const href = a.getAttribute('href');
                const match = href.match(/#!(.+)/);
                if (match) {
                    const hash = match[1];
                    if (!seen.has(hash)) {
                        seen.add(hash);
                        results.push({
                            title: a.textContent.trim(),
                            hash: hash,
                            url: href
                        });
                    }
                }
            }
            return results;
        }
    """)


# ---------------------------------------------------------------------------
# Step 2: Render each section to an individual PDF
# ---------------------------------------------------------------------------

async def render_section_to_pdf(
    browser: Browser,
    section: dict,
    output_dir: Path,
    index: int,
    total: int,
    semaphore: asyncio.Semaphore,
) -> Path | None:
    """Open a section page, wait for rendering, and save as a PDF.

    Returns the path to the generated PDF file, or None on failure.
    """
    async with semaphore:
        section_url = f"{INDEX_URL}#!{section['hash']}"
        pdf_path = output_dir / f"{index:04d}_{section['hash']}.pdf"

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            ctx = None
            try:
                log.info(
                    "[%d/%d] (attempt %d) Rendering: %s",
                    index + 1, total, attempt, section["title"][:80],
                )

                ctx = await browser.new_context()
                pg = await ctx.new_page()

                await pg.goto(
                    section_url,
                    wait_until="networkidle",
                    timeout=PAGE_LOAD_TIMEOUT,
                )
                await asyncio.sleep(CONTENT_SETTLE_DELAY)

                # Hide navigation/sidebar elements so only content prints
                await pg.evaluate("""
                    () => {
                        const hide = (sel) => {
                            document.querySelectorAll(sel).forEach(el => {
                                el.style.display = 'none';
                            });
                        };
                        // Common sidebar / nav selectors on citycode.net
                        hide('nav');
                        hide('.sidebar');
                        hide('.nav');
                        hide('.navbar');
                        hide('#sidebar');
                        hide('#navigation');
                        hide('.toc-panel');
                        hide('.toc-container');
                        hide('.header');
                        hide('header');
                        hide('footer');
                        hide('.footer');
                        hide('.search-bar');
                        hide('#search');
                        hide('.toolbar');
                        hide('.breadcrumb');

                        // Expand the content area to full width
                        const content = document.querySelector(
                            '.content, #content, .main-content, main, article, .code-content'
                        );
                        if (content) {
                            content.style.width = '100%';
                            content.style.maxWidth = '100%';
                            content.style.margin = '0';
                            content.style.padding = '20px';
                        }
                    }
                """)

                # Check if there's actual content on the page
                has_content = await pg.evaluate("""
                    () => {
                        const body = document.body;
                        return body && body.innerText.trim().length > 50;
                    }
                """)

                if not has_content:
                    log.warning(
                        "[%d/%d] Page appears empty: %s", index + 1, total, section["hash"]
                    )
                    await ctx.close()
                    return None

                await pg.pdf(
                    path=str(pdf_path),
                    format="Letter",
                    margin={"top": "0.5in", "right": "0.5in", "bottom": "0.5in", "left": "0.5in"},
                    print_background=True,
                )

                await ctx.close()
                log.info("[%d/%d] Saved: %s", index + 1, total, pdf_path.name)

                # Be polite: small delay between requests
                await asyncio.sleep(NAVIGATION_DELAY)
                return pdf_path

            except Exception as e:
                log.warning(
                    "[%d/%d] Error on attempt %d for '%s': %s",
                    index + 1, total, attempt, section["hash"], e,
                )
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                if attempt < RETRY_ATTEMPTS:
                    backoff = RETRY_BACKOFF * attempt
                    log.info("Retrying in %ds...", backoff)
                    await asyncio.sleep(backoff)
                else:
                    log.error(
                        "[%d/%d] Failed after %d attempts: %s",
                        index + 1, total, RETRY_ATTEMPTS, section["hash"],
                    )
                    return None


# ---------------------------------------------------------------------------
# Step 3: Merge individual PDFs into one consolidated document
# ---------------------------------------------------------------------------

def merge_pdfs(pdf_paths: list[Path], output_path: Path) -> None:
    """Merge a list of PDF files into a single output PDF."""
    writer = PdfWriter()
    for pdf_path in pdf_paths:
        if pdf_path and pdf_path.exists():
            try:
                writer.append(str(pdf_path))
            except Exception as e:
                log.warning("Could not merge %s: %s", pdf_path.name, e)

    if len(writer.pages) == 0:
        log.error("No pages to merge!")
        return

    writer.write(str(output_path))
    writer.close()
    log.info("Merged PDF saved to: %s (%d pages)", output_path, len(writer.pages))


# ---------------------------------------------------------------------------
# Step 4: Cleanup
# ---------------------------------------------------------------------------

def cleanup_temp_pdfs(temp_dir: Path) -> None:
    """Remove temporary individual PDF files."""
    for f in temp_dir.glob("*.pdf"):
        f.unlink()
    try:
        temp_dir.rmdir()
    except OSError:
        pass
    log.info("Cleaned up temporary files")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def run(output: str, temp_dir: str, keep_temp: bool, concurrency: int) -> None:
    output_path = Path(output).resolve()
    temp_path = Path(temp_dir).resolve()
    temp_path.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )

        # --- Discovery phase ---
        log.info("=" * 60)
        log.info("PHASE 1: Discovering all sections")
        log.info("=" * 60)

        ctx = await browser.new_context()
        discovery_page = await ctx.new_page()
        sections = await discover_sections(discovery_page)
        await ctx.close()

        if not sections:
            log.error("No sections discovered. The site structure may have changed.")
            await browser.close()
            sys.exit(1)

        # Print discovered sections
        for i, s in enumerate(sections):
            log.info("  %3d. %s", i + 1, s["title"][:100])

        # --- Rendering phase ---
        log.info("=" * 60)
        log.info("PHASE 2: Rendering %d sections to PDF", len(sections))
        log.info("=" * 60)

        semaphore = asyncio.Semaphore(concurrency)
        tasks = [
            render_section_to_pdf(browser, section, temp_path, i, len(sections), semaphore)
            for i, section in enumerate(sections)
        ]
        pdf_paths = await asyncio.gather(*tasks)

        await browser.close()

    # --- Merge phase ---
    log.info("=" * 60)
    log.info("PHASE 3: Merging PDFs")
    log.info("=" * 60)

    # Filter out None entries and sort by filename to maintain order
    valid_paths = [p for p in pdf_paths if p and p.exists()]
    valid_paths.sort(key=lambda p: p.name)

    if not valid_paths:
        log.error("No PDFs were generated. Nothing to merge.")
        sys.exit(1)

    log.info("Merging %d PDFs out of %d sections...", len(valid_paths), len(sections))
    merge_pdfs(valid_paths, output_path)

    # --- Cleanup ---
    if not keep_temp:
        cleanup_temp_pdfs(temp_path)

    log.info("=" * 60)
    log.info("DONE! Output: %s", output_path)
    log.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Scrape the De Soto Kansas City Code and generate a consolidated PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scraper.py
  python3 scraper.py -o my_output.pdf
  python3 scraper.py --keep-temp --concurrency 1
        """,
    )
    parser.add_argument(
        "-o", "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output PDF file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--temp-dir",
        default=DEFAULT_TEMP_DIR,
        help=f"Directory for temporary per-section PDFs (default: {DEFAULT_TEMP_DIR})",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary per-section PDFs after merging",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=MAX_CONCURRENCY,
        help=f"Max concurrent page renders (default: {MAX_CONCURRENCY})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    asyncio.run(run(args.output, args.temp_dir, args.keep_temp, args.concurrency))


if __name__ == "__main__":
    main()
