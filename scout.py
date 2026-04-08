#!/usr/bin/env python3
"""
Universal VC Startup Scout

Pipeline:
  scrape/load -> pre-filter -> coarse triage (by name) -> enrich (web search)
  -> score (with full descriptions) -> auto-categorize (optional)

Usage:
  python3 scout.py <scan_dir>                  # full pipeline
  python3 scout.py <scan_dir> --enrich-uncertain  # enrich+rescore companies that
                                                  # were left as name-only

The scan_dir must contain a config.json with:
  { name, url, profile, categories, api_key, [selector] }
"""

import json
import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone


# ---------- Progress ----------

def write_progress(scan_dir, stage, label, current=0, total=0, started_at=None, extra=None):
    started_at = started_at or datetime.now(timezone.utc).isoformat()
    elapsed = int((datetime.now(timezone.utc) - datetime.fromisoformat(started_at.replace("Z", "+00:00"))).total_seconds())
    progress = {
        "stage": stage,
        "stage_label": label,
        "current": current,
        "total": total,
        "percent": int(current / total * 100) if total > 0 else 0,
        "started_at": started_at,
        "elapsed_seconds": elapsed,
        **(extra or {}),
    }
    with open(os.path.join(scan_dir, "progress.json"), "w") as f:
        json.dump(progress, f)


# ---------- Step 1: Scrape ----------

def auto_detect_companies(soup):
    """Find the parent element whose direct children look most like a company list."""
    SKIP_WORDS = ["cookie", "privacy", "subscribe", "register", "newsletter",
                  "menu", "agenda", "speakers", "sponsor", "philosophy",
                  "media zone", "all rights", "terms of"]

    best_names = []
    best_count = 0

    for parent in soup.find_all(["div", "ul", "section", "ol"]):
        children = [c for c in parent.children if getattr(c, "name", None)]
        if len(children) < 30:
            continue
        names = []
        for c in children:
            t = c.get_text(strip=True)
            if not t or len(t) < 2 or len(t) > 100:
                continue
            if "\n" in t:
                continue
            if any(w in t.lower() for w in SKIP_WORDS):
                continue
            names.append(t)
        if len(names) > best_count and len(names) >= 30:
            best_count = len(names)
            best_names = names

    return best_names


def scrape_companies(url, selector=None):
    print(f"Fetching {url} ...")
    resp = requests.get(url, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    if selector:
        elements = soup.select(selector)
        names = [e.get_text(strip=True) for e in elements]
        if len(names) < 10:
            print(f"  Warning: selector '{selector}' returned {len(names)} results, falling back to auto-detect")
            names = auto_detect_companies(soup)
    else:
        names = auto_detect_companies(soup)

    # Deduplicate while preserving order
    seen, unique = set(), []
    for n in names:
        n = (n or "").strip()
        if not n or len(n) < 2:
            continue
        key = n.lower()
        if key not in seen:
            seen.add(key)
            unique.append(n)
    return unique


# ---------- Step 2: Pre-filter ----------

KNOWN_LARGE = {
    "amazon", "amazon web services", "aws", "apple", "walmart", "target",
    "google", "google cloud", "alphabet", "meta", "facebook",
    "microsoft", "oracle", "sap", "ibm", "cisco", "intel", "dell", "hp",
    "samsung", "lg", "sony", "nike", "adidas", "coca-cola", "pepsi",
    "procter & gamble", "unilever", "nestle", "johnson & johnson",
    "pfizer", "merck", "general electric", "general motors", "ford",
    "toyota", "honda", "boeing", "lockheed martin", "raytheon",
    "caterpillar", "deere", "home depot", "walgreens", "cvs",
    "mcdonald's", "starbucks", "disney", "comcast", "at&t", "verizon",
    "t-mobile", "wells fargo", "jpmorgan", "bank of america",
    "goldman sachs", "morgan stanley", "visa", "mastercard", "paypal",
    "salesforce", "adobe", "vmware", "uber", "lyft", "airbnb",
    "nvidia", "broadcom", "qualcomm", "amd", "texas instruments",
    "accenture", "deloitte", "pwc", "kpmg", "ey", "mckinsey", "bcg", "bain",
    "gartner", "idc", "forrester",
    "openai", "anthropic", "deepmind", "tesla",
    "jpmorgan chase", "citigroup", "capital one", "american express",
    "blackrock", "fidelity", "charles schwab", "morgan stanley",
    "andreessen horowitz", "a16z", "sequoia capital", "sequoia",
    "khosla ventures", "general catalyst", "accel", "index ventures",
    "bessemer venture partners", "gv", "kleiner perkins",
    "lightspeed venture partners", "greylock", "benchmark",
    "tiger global", "softbank", "insight partners",
    "mercedes-benz", "bmw", "volkswagen",
}

MATURE_PATTERNS = [
    r"\b(association|council|institute|foundation|university|college)\b",
    r"\b(government|federal|state|county|city|department|agency)\b",
    r"\b(consulting|advisory|advisors|partners llp)\b",
]


def is_likely_startup(name):
    lower = name.lower().strip()
    clean = re.sub(r"\s*(,?\s*(inc\.?|llc|ltd|corp\.?|co\.?|group|holdings?))\s*$", "", lower, flags=re.IGNORECASE).strip()
    if clean in KNOWN_LARGE:
        return False
    for pattern in MATURE_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            return False
    return True


# ---------- Step 3: Coarse triage (cheap, by-name) ----------

def call_claude(api_key, body, timeout=120):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        },
        json=body,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def parse_json_text(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


class TriageFailure(Exception):
    """Raised when every triage batch failed (e.g. bad API key, rate limit)."""


def batch_triage(companies, config, scan_dir, started_at, batch_size=200):
    """Coarse triage by name only. Permissive — meant to drop only obvious mismatches.
    Real scoring happens later, after enrichment, with full descriptions."""
    api_key = config["api_key"]
    profile = config["profile"]
    categories = config["categories"]
    cat_str = ", ".join(f'"{c}"' for c in categories)

    all_results = []
    total_batches = (len(companies) + batch_size - 1) // batch_size
    failed_batches = 0
    last_error = None

    for i in range(0, len(companies), batch_size):
        batch = companies[i:i + batch_size]
        batch_num = i // batch_size + 1
        write_progress(scan_dir, "triaging", "Coarse triage by name",
                       batch_num, total_batches, started_at, {"matches_so_far": len(all_results)})
        print(f"\nTriage batch {batch_num}/{total_batches}: {len(batch)} companies...")

        synopsis_block = ""
        if config.get("synopsis"):
            synopsis_block = f"\nCONFERENCE CONTEXT:\n{config['synopsis']}\n"

        prompt = f"""You are doing a CHEAP first-pass triage of conference attendees for a VC. We will enrich and re-score the survivors later with real descriptions, so be PERMISSIVE here — only drop the obvious mismatches.

VC PROFILE / THESIS:
{profile}
{synopsis_block}
For each company below, judge based on the NAME ALONE (use the conference context above to disambiguate generic names — e.g., at a maritime conference "Keel" almost certainly means a maritime company, not a beer company):
- DROP it if the name clearly indicates a mature corporate (Fortune 500), bank, ad agency, consulting firm, association, university, government body, or VC fund.
- KEEP it (with relevance 3-10) if the name COULD be relevant to the VC's thesis, OR if you can't tell from the name (unknown / generic name).

Be GENEROUS — when in doubt, keep it. Final scoring happens later with real data.

COMPANIES ({len(batch)}):
{chr(10).join(f"- {c}" for c in batch)}

Respond with a JSON array. Include only companies you're KEEPING. For each:
- "name": exact company name
- "type": "startup" | "growth" | "unknown"
- "relevance": 3-10 (3 = "name is opaque, can't tell, but include for safety"; 7+ = "name strongly suggests fit")
- "category": one of: {cat_str}  (best guess from name alone)
- "reason": 1-sentence explanation (what you guess they do)

Drop anything with type "mature" or that's clearly not a startup.
Respond ONLY with the JSON array. If nothing kept, return []."""

        body = {
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            result = call_claude(api_key, body)
            text = result["content"][0]["text"]
            matches = parse_json_text(text)
            all_results.extend(matches)
            print(f"  Kept {len(matches)} companies after triage")
        except Exception as e:
            failed_batches += 1
            last_error = str(e)
            print(f"  ERROR processing batch: {e}")

        if i + batch_size < len(companies):
            time.sleep(1)

    # If every batch failed, surface that as a hard error instead of silently
    # producing an empty results list. Common causes: bad API key, no credit,
    # rate limit, network failure.
    if failed_batches == total_batches and total_batches > 0:
        raise TriageFailure(
            f"All {total_batches} triage batches failed. Last error: {last_error}. "
            f"Common causes: invalid Anthropic API key, no credit, rate limit, or network failure."
        )

    return all_results


# ---------- Synopsis (pipeline context) ----------

def generate_synopsis(config, scan_dir, started_at):
    """One-shot Claude call: produce a 2-3 sentence description of the conference,
    used downstream as disambiguation context for ambiguous company names."""
    api_key = config["api_key"]
    name = config.get("name", "this conference")
    url = config.get("url", "")
    write_progress(scan_dir, "synopsis", f"Generating conference synopsis for {name}",
                   started_at=started_at)
    print(f"\nGenerating synopsis for {name}...")

    prompt = f"""Describe this conference in 2-3 sentences for downstream context.

Conference name: {name}
Conference URL: {url or "(no URL — companies were uploaded as CSV)"}

Cover: sector/industry, typical attendee type, and the kinds of companies that attend. Be specific and factual. If you don't know this specific conference, infer reasonably from the name. No preamble, no markdown — just the description."""

    try:
        result = call_claude(api_key, {
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
        })
        text_parts = [b["text"] for b in result["content"] if b.get("type") == "text"]
        synopsis = " ".join(text_parts).strip()
        print(f"  → {synopsis}")
        return synopsis
    except Exception as e:
        print(f"  Could not generate synopsis: {e}")
        return ""


# ---------- Step 4: Enrich (web search) ----------

def enrich_one(name, api_key, profile_summary, hint=None, synopsis=None, use_search=True):
    """Enrich one company. `hint` and `synopsis` are passed to disambiguate
    generic names like "Keel" or "Beehive". `use_search=False` skips the
    web_search tool — used as a fallback when web search returns no text."""
    context_lines = []
    if synopsis:
        context_lines.append(f"Conference context: {synopsis}")
    if hint:
        context_lines.append(f"Initial guess about this company: {hint}")
    context = ("\n" + "\n".join(context_lines) + "\n") if context_lines else ""

    instruction = (
        f'Search the web for the company "{name}".' if use_search
        else f'Describe the company "{name}" using your training data (no web search needed).'
    )

    user_msg = f"""{instruction}{context}
Use the context above to disambiguate which company this is if the name is generic.

Find:
1. What they do (1-2 sentences)
2. Founded year
3. Funding raised (if any)
4. Stage (seed/A/B/C/growth/public)
5. HQ location
6. Why they're innovative (relevant to: {profile_summary})

Respond as JSON: {{"name": "...", "description": "...", "founded": "...", "funding": "...", "stage": "...", "hq": "...", "innovation": "...", "url": "..."}}
Only JSON, no other text. If you genuinely cannot find anything, return {{"description": "Unknown — could not find specific information"}}."""

    body = {
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": user_msg}],
    }
    if use_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]

    result = call_claude(api_key, body, timeout=90)
    text_parts = [b["text"] for b in result["content"] if b.get("type") == "text"]
    text = " ".join(text_parts).strip()

    if not text:
        raise ValueError(
            "Claude returned only tool_use blocks (no text). "
            "Likely ambiguous search results — needs disambiguation hint or knowledge-only fallback."
        )

    return parse_json_text(text)


def enrich_companies(candidates, config, scan_dir, started_at, label="Enriching companies"):
    """Enrich every company in `candidates` via web search. Skips already-enriched.
    Saves results.json incrementally so a long enrich can be paused/cancelled
    without losing progress."""
    api_key = config["api_key"]
    profile_summary = config["profile"][:300]
    synopsis = config.get("synopsis") or None
    results_path = os.path.join(scan_dir, "results.json")

    print(f"\n{label}: {len(candidates)} companies via web search...")

    enriched = []
    for i, company in enumerate(candidates):
        write_progress(scan_dir, "enriching", label, i + 1, len(candidates), started_at)
        print(f"  [{i+1}/{len(candidates)}] {company['name']}...")
        if company.get("description"):
            enriched.append(company)
            continue

        hint = company.get("reason") or None
        info = None
        last_err = None

        # Pass 1: web search + retries with backoff
        for attempt in range(3):
            try:
                info = enrich_one(
                    company["name"], api_key, profile_summary,
                    hint=hint, synopsis=synopsis, use_search=True,
                )
                break
            except Exception as e:
                last_err = e
                print(f"    Attempt {attempt+1}/3 (web search) failed: {e}")
                time.sleep(1 + attempt * 2)  # 1s, 3s

        # Pass 2: knowledge-only fallback if web search exhausted all retries.
        # Helps with ambiguous names like "Keel" where search returns mixed
        # results across multiple companies and Claude refuses to commit.
        if info is None:
            print(f"    Falling back to knowledge-only enrichment...")
            try:
                info = enrich_one(
                    company["name"], api_key, profile_summary,
                    hint=hint, synopsis=synopsis, use_search=False,
                )
                print(f"    Knowledge-only fallback succeeded.")
            except Exception as e:
                last_err = e
                print(f"    Knowledge-only fallback also failed: {e}")

        if info is not None:
            # Never let Claude's response overwrite the original name (it sometimes
            # returns a slightly different spelling, which breaks the merge step).
            info.pop("name", None)
            info["enriched_at"] = datetime.now(timezone.utc).isoformat()
            enriched.append({**company, **info})
        else:
            print(f"    Giving up on {company['name']}: {last_err}")
            enriched.append(company)

        # Incremental save every 10 companies so partial results survive cancel
        if (i + 1) % 10 == 0 and os.path.exists(results_path):
            try:
                with open(results_path) as f:
                    data = json.load(f)
                by_name = {c["name"]: c for c in enriched}
                data["results"] = [by_name.get(r["name"], r) for r in data["results"]]
                with open(results_path, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass

        time.sleep(0.5)

    return enriched


# ---------- Step 5: Score with full descriptions ----------

def score_with_descriptions(companies, config, scan_dir, started_at, batch_size=50):
    """Re-score every company using their full enriched description.
    This is the REAL score — the triage step was just a coarse keeper."""
    api_key = config["api_key"]
    profile = config["profile"]

    total_batches = (len(companies) + batch_size - 1) // batch_size
    rescored = []

    for i in range(0, len(companies), batch_size):
        batch = companies[i:i + batch_size]
        batch_num = i // batch_size + 1
        write_progress(scan_dir, "scoring", "Scoring with full context",
                       batch_num, total_batches, started_at)
        print(f"\nScoring batch {batch_num}/{total_batches} ({len(batch)} companies)...")

        summaries = []
        for c in batch:
            desc = c.get("description") or c.get("reason") or ""
            funding = c.get("funding", "")
            stage = c.get("stage", c.get("type", ""))
            summaries.append(f'- {c["name"]} | {stage} | {funding} | {desc[:300]}')

        prompt = f"""You are scoring companies against a VC's thesis using their full enriched descriptions. This is the REAL score — be discriminating.

VC PROFILE / THESIS:
{profile}

SCORING RUBRIC:
- 9-10 PERFECT FIT (must meet): truly novel idea, strong technical moat, large TAM, right stage, clearly aligned with the thesis.
- 7-8  STRONG FIT (worth a conversation): innovative approach to a known problem, clear differentiation, moderate moat, large TAM.
- 5-6  WEAK FIT (skip unless time): incremental improvement, low defensibility, crowded market, or only tangentially related to the thesis.
- 3-4  NOT A FIT: wrong sector, wrong stage, generic, or thesis mismatch.
- 1-2  IRRELEVANT: clearly outside scope.

Be HARSH. Most companies are 5-6. Very few should be 9-10.

COMPANIES ({len(batch)}):
{chr(10).join(summaries)}

For each company, return a JSON object:
- "name": exact company name as provided
- "relevance": 1-10
- "reason": 1-sentence justification (what they do + why this score)

Respond ONLY with the JSON array."""

        body = {
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            result = call_claude(api_key, body)
            scores = parse_json_text(result["content"][0]["text"])
            by_name = {s["name"]: s for s in scores}
            for c in batch:
                if c["name"] in by_name:
                    s = by_name[c["name"]]
                    if "relevance" in s:
                        c["relevance"] = s["relevance"]
                    if s.get("reason"):
                        c["reason"] = s["reason"]
                rescored.append(c)
            print(f"  Scored {sum(1 for c in batch if c['name'] in by_name)}/{len(batch)}")
        except Exception as e:
            print(f"  ERROR scoring batch: {e}")
            rescored.extend(batch)

        if i + batch_size < len(companies):
            time.sleep(1)

    return rescored


# ---------- Step 6: Auto-generate categories ----------

def auto_categorize(enriched, config, scan_dir, started_at):
    """Look at enriched companies and ask Claude to propose categories that fit
    THIS specific dataset, then re-bucket every company."""
    api_key = config["api_key"]
    profile = config["profile"]

    summaries = []
    for c in enriched:
        desc = c.get("description") or c.get("reason") or ""
        summaries.append(f'- {c["name"]}: {desc[:200]}')

    write_progress(scan_dir, "categorizing", "Auto-generating categories", 1, 2, started_at)
    print(f"\nAuto-generating categories from {len(enriched)} companies...")

    cat_prompt = f"""You are organizing a VC's pipeline of companies they discovered at a conference.

VC PROFILE:
{profile}

Below are the companies that matched the thesis. Propose 8-12 useful categories that cleanly bucket THIS specific set of companies — not generic categories, but ones that reflect what's actually here. Categories should be distinct, useful for the VC to filter by, and roughly balanced in size.

COMPANIES:
{chr(10).join(summaries[:300])}

Respond with ONLY a JSON array of category names: ["Category 1", "Category 2", ...]
"""
    try:
        result = call_claude(api_key, {
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": cat_prompt}],
        })
        new_categories = parse_json_text(result["content"][0]["text"])
        print(f"  Generated {len(new_categories)} categories: {', '.join(new_categories)}")
    except Exception as e:
        print(f"  ERROR generating categories: {e}")
        return enriched, config["categories"]

    # Re-bucket every company in batches
    write_progress(scan_dir, "categorizing", "Re-categorizing companies", 2, 2, started_at)
    cat_str = ", ".join(f'"{c}"' for c in new_categories)
    bucket_size = 100
    rebucketed = []
    for i in range(0, len(enriched), bucket_size):
        batch = enriched[i:i + bucket_size]
        batch_summaries = []
        for c in batch:
            desc = c.get("description") or c.get("reason") or ""
            batch_summaries.append(f'- {c["name"]}: {desc[:200]}')
        prompt = f"""Categorize each company below into ONE of these categories:
{cat_str}

COMPANIES:
{chr(10).join(batch_summaries)}

Respond with a JSON array: [{{"name": "...", "category": "..."}}, ...]
Use exact company names. Pick the best-fitting category for each."""
        try:
            result = call_claude(api_key, {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            })
            assignments = parse_json_text(result["content"][0]["text"])
            by_name = {a["name"]: a.get("category") for a in assignments}
            for c in batch:
                if c["name"] in by_name and by_name[c["name"]]:
                    c["category"] = by_name[c["name"]]
                rebucketed.append(c)
        except Exception as e:
            print(f"  ERROR re-categorizing batch: {e}")
            rebucketed.extend(batch)
        time.sleep(0.5)

    return rebucketed, new_categories


# ---------- Main ----------

def save_results(scan_dir, output):
    with open(os.path.join(scan_dir, "results.json"), "w") as f:
        json.dump(output, f, indent=2)


def strip_api_key(config_path, config):
    """Persist config without the api_key (we don't keep keys on disk)."""
    config_to_save = {k: v for k, v in config.items() if k != "api_key"}
    with open(config_path, "w") as f:
        json.dump(config_to_save, f, indent=2)


def run_enrich_uncertain(scan_dir, config, config_path):
    """Enrich the companies that were left as name-only by the initial scan,
    then re-score them with full context, and merge back into results.json."""
    started_at = datetime.now(timezone.utc).isoformat()
    results_path = os.path.join(scan_dir, "results.json")
    if not os.path.exists(results_path):
        print("ERROR: results.json not found, nothing to enrich")
        sys.exit(1)
    with open(results_path) as f:
        data = json.load(f)

    unenriched = [c for c in data["results"] if not c.get("description")]
    if not unenriched:
        print("Nothing to enrich — every company already has a description")
        write_progress(scan_dir, "done", "Nothing to enrich", started_at=started_at)
        strip_api_key(config_path, config)
        return

    print(f"Enriching {len(unenriched)} uncertain candidates...")
    enriched = enrich_companies(unenriched, config, scan_dir, started_at,
                                label=f"Enriching {len(unenriched)} uncertain candidates")

    # Re-score the newly-enriched
    rescored = score_with_descriptions(enriched, config, scan_dir, started_at)

    # Merge back
    rescored_by_name = {c["name"]: c for c in rescored}
    new_results = [rescored_by_name.get(r["name"], r) for r in data["results"]]
    new_results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    data["results"] = new_results
    data["enriched"] = sum(1 for c in new_results if c.get("description"))
    save_results(scan_dir, data)

    strip_api_key(config_path, config)
    write_progress(scan_dir, "done", "Complete", started_at=started_at)
    print(f"\nDone! {len(unenriched)} companies enriched and rescored.")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scout.py <scan_dir> [--enrich-uncertain]")
        sys.exit(1)

    scan_dir = sys.argv[1]
    config_path = os.path.join(scan_dir, "config.json")
    if not os.path.exists(config_path):
        print(f"ERROR: {config_path} not found")
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    if not config.get("api_key"):
        print("ERROR: config.json missing api_key")
        sys.exit(1)

    if "--enrich-uncertain" in sys.argv:
        run_enrich_uncertain(scan_dir, config, config_path)
        return

    started_at = datetime.now(timezone.utc).isoformat()

    # Step 1: Get companies — from companies.json (CSV upload) or by scraping URL
    companies_path = os.path.join(scan_dir, "companies.json")
    if os.path.exists(companies_path):
        write_progress(scan_dir, "loading", "Loading companies from upload", started_at=started_at)
        with open(companies_path) as f:
            all_companies = json.load(f)
        print(f"Loaded {len(all_companies)} companies from upload")
    else:
        write_progress(scan_dir, "scraping", f"Scraping {config['url']}", started_at=started_at)
        all_companies = scrape_companies(config["url"], config.get("selector"))
        print(f"Scraped {len(all_companies)} unique companies")

    if not all_companies:
        msg = (
            f"Couldn't find any company names on {config.get('url') or 'this page'}. "
            f"The page is likely JavaScript-rendered (the company list loads dynamically "
            f"after the page opens, so a static fetch sees nothing). "
            f"Workarounds: (1) upload a CSV/TXT of the company names instead, "
            f"or (2) view the page source in your browser, find the element wrapping the "
            f"company names, and provide a CSS selector under \"Advanced\" in the New Scan modal."
        )
        write_progress(scan_dir, "error", msg, started_at=started_at, extra={"error_detail": msg})
        print(f"ERROR: {msg}")
        strip_api_key(config_path, config)
        sys.exit(1)

    # Step 2: Pre-filter
    write_progress(scan_dir, "filtering", "Pre-filtering known large companies", started_at=started_at)
    candidates = [c for c in all_companies if is_likely_startup(c)]
    print(f"Pre-filtered: {len(all_companies) - len(candidates)} dropped")
    print(f"Remaining candidates: {len(candidates)}")

    # Step 2.5: Generate conference synopsis (used as disambiguation context
    # downstream by triage and enrichment for ambiguous company names).
    if not config.get("synopsis"):
        synopsis = generate_synopsis(config, scan_dir, started_at)
        if synopsis:
            config["synopsis"] = synopsis
            # Persist immediately so the UI can show it during the long triage step.
            # strip_api_key only touches the file on disk; the in-memory config
            # still has api_key for downstream Claude calls.
            strip_api_key(config_path, config)

    # Step 3: Coarse triage (cheap, by name only)
    try:
        triaged = batch_triage(candidates, config, scan_dir, started_at)
    except TriageFailure as e:
        write_progress(scan_dir, "error", str(e), started_at=started_at, extra={"error_detail": str(e)})
        print(f"ERROR: {e}")
        strip_api_key(config_path, config)
        sys.exit(1)
    print(f"\nTriaged: {len(triaged)} candidates kept")

    # Save triaged results immediately so the UI shows something while enrichment runs
    save_results(scan_dir, {
        "total_scraped": len(all_companies),
        "pre_filtered": len(candidates),
        "triaged": len(triaged),
        "enriched": 0,
        "results": sorted(triaged, key=lambda x: x.get("relevance", 0), reverse=True),
    })

    # Step 4: Enrich the high-confidence ones (triage relevance >= 5).
    # Lower-relevance ones are kept as name-only and can be enriched on demand.
    high_confidence = [c for c in triaged if c.get("relevance", 0) >= 5]
    print(f"\nEnriching {len(high_confidence)} high-confidence matches "
          f"(relevance >= 5). {len(triaged) - len(high_confidence)} uncertain "
          f"candidates left as name-only — enrich on demand.")
    enriched = enrich_companies(high_confidence, config, scan_dir, started_at,
                                label=f"Enriching {len(high_confidence)} high-confidence matches")

    # Step 5: Re-score with full descriptions (this is the REAL score)
    if enriched:
        scored = score_with_descriptions(enriched, config, scan_dir, started_at)
    else:
        scored = []

    # Merge: scored enriched companies + untouched uncertain ones
    scored_by_name = {c["name"]: c for c in scored}
    all_results = [scored_by_name.get(c["name"], c) for c in triaged]

    # Step 6: Auto-categorize (optional)
    if config.get("auto_categorize") and all_results:
        all_results, new_cats = auto_categorize(all_results, config, scan_dir, started_at)
        config["categories"] = new_cats

    output = {
        "total_scraped": len(all_companies),
        "pre_filtered": len(candidates),
        "triaged": len(triaged),
        "enriched": len(enriched),
        "results": sorted(all_results, key=lambda x: x.get("relevance", 0), reverse=True),
    }
    save_results(scan_dir, output)

    config["completed_at"] = datetime.now(timezone.utc).isoformat()
    strip_api_key(config_path, config)

    write_progress(scan_dir, "done", "Complete", started_at=started_at, extra={
        "total_scraped": len(all_companies),
        "triaged": len(triaged),
        "enriched": len(enriched),
    })

    print(f"\nDone! Results saved to {scan_dir}/results.json")


if __name__ == "__main__":
    main()
