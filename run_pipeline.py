"""
run_pipeline.py
===============
Top-level entry point for the Pharma News Intelligence Pipeline.

TWO MODES:

  --install   (run once per new portal)
    Calls install.py to:
      Step 1 -- search URL discovery  (discovery.py + Groq)
      Step 2 -- extraction parser     (Groq writes extract_<domain>())
      Step 3 -- article scraper       (Groq writes article_<domain>())
    Writes: search_registry.json, search_engines.py,
            extractor_registry.json, extraction_portals.py

  (default)   (run daily / on-demand)
    Calls extraction.py to:
      Stage 1 -- navigate to search page   (search_engines.py)
      Stage 2 -- extract article links     (extraction_portals.py)
      Stage 3 -- crawl pages
      Stage 4 -- filter to last N days
      Stage 5 -- scrape article bodies     (extraction_portals.py)
      Stage 6 -- save per-domain JSON
    Then merges all *_results.json → merged_articles.json
    Then calls SUMMARIZER to produce a MONTHLY PHARMA INTELLIGENCE BRIEF

Usage (terminal):
  # Installation (run once to set up portals):
  python run_pipeline.py --install
  python run_pipeline.py --install --url biopharmadive.com
  python run_pipeline.py --install --url newsite.com --query crispr

  # Run extraction + merge + summarize (daily use):
  python run_pipeline.py
  python run_pipeline.py --query crispr --days 14
  python run_pipeline.py --domain biopharmadive.com --query protac
  python run_pipeline.py --limit 5 --no-enrich
  python run_pipeline.py --no-summarize          (skip summarizer step)

Usage (Jupyter):
  # Install:
  await run_pipeline(install=True)
  await run_pipeline(install=True, url="biopharmadive.com")

  # Run:
  await run_pipeline()
  await run_pipeline(query="crispr", days=14)
  await run_pipeline(query="protac", summarize=False)
"""

# =============================================================================
#  CONFIG
# =============================================================================

QUERY            = "protac"
DATE_WINDOW_DAYS = 7
ENRICH_ARTICLES  = True
OUTPUT_DIR       = "extraction_output"
MERGED_FILE      = "merged_articles.json"   # flat list, input to summarizer
BRIEF_FILE       = "pharma_brief.txt"       # final SUMMARIZER output

# =============================================================================
#  IMPORTS
# =============================================================================

import argparse, asyncio, json, time, logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# =============================================================================
#  MERGE  (replaces standalone merge.py)
# =============================================================================

def merge_results(query: str, days: int) -> list:
    """
    Read all *_results.json from OUTPUT_DIR and flatten into a single list
    of article dicts.  Writes merged_articles.json alongside the domain files.

    Each article gets a top-level 'domain' field so the summarizer can cite
    its source.

    Returns the flat list (may be empty if no per-domain files exist).
    """
    output_path = Path(OUTPUT_DIR)
    all_articles: list = []

    for fpath in sorted(output_path.glob("*_results.json")):
        domain_key = fpath.stem.replace("_results", "").replace("_", ".")
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"[merge] Could not read {fpath.name}: {e}")
            continue

        # data is {month: {article_count, articles: [...]}, ...}
        for month, month_data in data.items():
            for art in month_data.get("articles", []):
                art.setdefault("domain", domain_key)
                art.setdefault("period", month)
                all_articles.append(art)

    merged_path = output_path / MERGED_FILE
    # Always overwrite — never accumulate stale articles
    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "query":          query,
                "date_window":    f"last {days} days",
                "merged_at":      datetime.now(timezone.utc).isoformat(),
                "total_articles": len(all_articles),
                "articles":       all_articles,
            },
            f, indent=2,
        )

    print(f"\n[merge] {len(all_articles)} articles → {merged_path}")
    return all_articles


# =============================================================================
#  SUMMARIZER RUNNER  (calls SUMMARIZER.py logic inline)
# =============================================================================

def run_summarizer(articles: list, query: str, output_path: str) -> str | None:
    """
    Inline equivalent of:  python SUMMARIZER.py --input ... --query ... --output ...

    Imports SUMMARIZER at runtime so it doesn't need to be on sys.path at
    module-load time.  Returns the final brief text, or None on failure.
    """
    if not articles:
        print("[summarizer] No articles to summarize — skipping.")
        return None

    try:
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("SUMMARIZER", "SUMMARIZER.py")
        if spec is None:
            raise ImportError("SUMMARIZER.py not found in working directory.")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["SUMMARIZER"] = mod
        spec.loader.exec_module(mod)
    except ImportError as e:
        print(f"[summarizer] ERROR: {e}")
        return None

    # chunk_articles and call_nvidia_api are top-level functions in SUMMARIZER.py
    chunks = list(mod.chunk_articles(articles, chunk_size=5))
    print(f"[summarizer] {len(articles)} articles in {len(chunks)} chunks — streaming...\n")
    print("─" * 65)

    all_summaries = []
    for idx, chunk in enumerate(chunks, 1):
        print(f"[summarizer] Chunk {idx}/{len(chunks)}")
        chunk_prompt = mod.build_combined_prompt(chunk, query)
        try:
            summary = mod.call_nvidia_api(mod.SYSTEM_PROMPT, chunk_prompt, query)
            if summary:
                all_summaries.append(summary)
        except Exception as e:
            print(f"[summarizer] Chunk {idx} failed: {e}")

    if not all_summaries:
        print("[summarizer] All chunks failed — no brief produced.")
        return None

    print("\n[summarizer] Generating final combined brief...\n")
    final_prompt = (
        "You are given multiple partial pharmaceutical summaries.\n\n"
        "Combine them into ONE unified MONTHLY PHARMA INTELLIGENCE BRIEF.\n\n"
        + "\n\n".join(all_summaries)
    )
    final_brief = mod.call_nvidia_api(mod.SYSTEM_PROMPT, final_prompt, query)
    print("─" * 65)

    if not final_brief:
        print("[summarizer] Empty final response.")
        return None

    full_output = mod.write_output(final_brief, query, len(articles), output_path)
    print(f"[summarizer] Brief saved → {output_path}")
    return full_output


# =============================================================================
#  MAIN
# =============================================================================

async def run_pipeline(
    install:      bool       = False,
    url:          str | None = None,    # single-domain target (install or extract)
    query:        str        = QUERY,
    days:         int        = DATE_WINDOW_DAYS,
    enrich:       bool       = ENRICH_ARTICLES,
    limit:        int | None = None,
    skip_search:  bool       = False,   # install mode: skip Step 1
    skip_article: bool       = False,   # install mode: skip Step 3
    resume:       bool       = True,    # install mode: skip already-installed
    domain:       str | None = None,    # extract mode: process only this domain
    summarize:    bool       = True,    # extract mode: run summarizer after merge
):
    """
    Main entry point.

    Set install=True to run the installation phase.
    Leave install=False (default) to run extraction → merge → summarize.
    """
    t0 = time.time()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if install:
        # ── INSTALL MODE ──────────────────────────────────────────────────────
        print(f"\n{'='*65}")
        print(f"  PHARMA PIPELINE -- INSTALL MODE")
        print(f"  Query   : {query!r}")
        print(f"  Target  : {url or 'all portals in articles_clear_info.json'}")
        print(f"  Started : {started}")
        print(f"{'='*65}")

        try:
            from install import install as run_install
        except ImportError:
            print("  ERROR: install.py not found in working directory.")
            return

        await run_install(
            url          = url,
            query        = query,
            limit        = limit,
            resume       = resume,
            skip_search  = skip_search,
            skip_article = skip_article,
        )

    else:
        # ── EXTRACTION MODE ───────────────────────────────────────────────────
        target_domain = domain or url   # accept either kwarg for convenience
        print(f"\n{'='*65}")
        print(f"  PHARMA PIPELINE -- EXTRACTION + MERGE + SUMMARIZE")
        print(f"  Query       : {query!r}")
        print(f"  Date window : last {days} days")
        print(f"  Domain      : {target_domain or 'all installed portals'}")
        print(f"  Enrich      : {enrich}")
        print(f"  Summarize   : {summarize}")
        print(f"  Started     : {started}")
        print(f"{'='*65}")

        # ── Stage A: Extraction ───────────────────────────────────────────────
        try:
            from extraction import main as run_extraction
        except ImportError:
            print("  ERROR: extraction.py not found in working directory.")
            return

        await run_extraction(
            query  = query,
            domain = target_domain,
            limit  = limit,
            enrich = enrich,
            days   = days,
        )

        # ── Stage B: Merge ────────────────────────────────────────────────────
        print(f"\n{'='*65}")
        print(f"  STAGE B -- MERGE")
        print(f"{'='*65}")
        articles = merge_results(query=query, days=days)

        if not articles:
            print("\n[merge] No articles found — nothing to summarize.")
            elapsed = round((time.time() - t0) / 60, 1)
            print(f"\n  Total time: {elapsed} minutes")
            return

        # ── Stage C: Summarize ────────────────────────────────────────────────
        if summarize:
            print(f"\n{'='*65}")
            print(f"  STAGE C -- SUMMARIZE  ({len(articles)} articles, query={query!r})")
            print(f"{'='*65}")
            brief_path = str(Path(OUTPUT_DIR) / BRIEF_FILE)
            run_summarizer(articles=articles, query=query, output_path=brief_path)
        else:
            print("\n[summarize] Skipped (--no-summarize).")

    elapsed = round((time.time() - t0) / 60, 1)
    print(f"\n  Total time: {elapsed} minutes")


# =============================================================================
#  CLI
# =============================================================================

def _build_parser():
    p = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="Pharma News Intelligence Pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
--- INSTALL (run once per new portal) ---
  python run_pipeline.py --install
  python run_pipeline.py --install --url biopharmadive.com
  python run_pipeline.py --install --url newsite.com --query crispr
  python run_pipeline.py --install --limit 5          (install first 5 portals)
  python run_pipeline.py --install --no-resume         (reinstall all)
  python run_pipeline.py --install --skip-search       (extraction parser only)
  python run_pipeline.py --install --skip-article      (no article scraper)

--- EXTRACT + MERGE + SUMMARIZE (daily use) ---
  python run_pipeline.py
  python run_pipeline.py --query crispr --days 14
  python run_pipeline.py --domain biopharmadive.com --query protac
  python run_pipeline.py --limit 5 --no-enrich
  python run_pipeline.py --no-summarize               (skip brief generation)
        """,
    )

    # Mode
    p.add_argument(
        "--install", action="store_true", default=False,
        help="Run installation phase (discover + generate parsers). "
             "Without this flag, runs extraction → merge → summarize."
    )

    # Shared
    p.add_argument("--url",    "-u", default=None,
                   help="Single domain to install or extract")
    p.add_argument("--query",  "-q", default=QUERY,
                   help=f"Search query (default: {QUERY!r})")
    p.add_argument("--limit",  "-n", type=int, default=None,
                   help="Cap number of domains")

    # Extract-mode options
    p.add_argument("--domain", "-d", default=None,
                   help="[Extract mode] Process only this domain")
    p.add_argument("--days",         type=int, default=DATE_WINDOW_DAYS,
                   help=f"[Extract mode] Date filter in days (default: {DATE_WINDOW_DAYS})")
    p.add_argument("--enrich", dest="enrich",
                   action=argparse.BooleanOptionalAction, default=ENRICH_ARTICLES,
                   help="[Extract mode] Scrape full article bodies")
    p.add_argument("--summarize", dest="summarize",
                   action=argparse.BooleanOptionalAction, default=True,
                   help="[Extract mode] Run SUMMARIZER after merge (default: --summarize)")

    # Install-mode options
    p.add_argument("--skip-search",  action="store_true", default=False,
                   help="[Install mode] Skip Step 1 (search discovery)")
    p.add_argument("--skip-article", action="store_true", default=False,
                   help="[Install mode] Skip Step 3 (article scraper)")
    p.add_argument("--resume", dest="resume",
                   action=argparse.BooleanOptionalAction, default=True,
                   help="[Install mode] Skip already-installed domains (default: --resume)")

    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    asyncio.run(run_pipeline(
        install      = args.install,
        url          = args.url,
        query        = args.query,
        days         = args.days,
        enrich       = args.enrich,
        limit        = args.limit,
        skip_search  = args.skip_search,
        skip_article = args.skip_article,
        resume       = args.resume,
        domain       = args.domain,
        summarize    = args.summarize,
    ))
else:
    print("OK  run_pipeline.py loaded.")
    print()
    print("  INSTALL (run once):")
    print("    await run_pipeline(install=True)")
    print("    await run_pipeline(install=True, url='biopharmadive.com')")
    print("    await run_pipeline(install=True, url='newsite.com', query='crispr')")
    print()
    print("  EXTRACT + MERGE + SUMMARIZE (daily):")
    print("    await run_pipeline()")
    print("    await run_pipeline(query='crispr', days=14)")
    print("    await run_pipeline(domain='biopharmadive.com')")
    print("    await run_pipeline(query='protac', summarize=False)")