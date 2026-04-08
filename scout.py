#!/usr/bin/env python3
"""
Universal VC Startup Scout
Runs the full scrape -> filter -> classify -> enrich pipeline for a single scan.

Usage:
  python3 scout.py <scan_dir>

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


# ---------- Step 3: Classify ----------

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


def batch_classify(companies, config, scan_dir, started_at, batch_size=200):
    api_key = config["api_key"]
    profile = config["profile"]
    categories = config["categories"]
    cat_str = ", ".join(f'"{c}"' for c in categories)

    all_results = []
    total_batches = (len(companies) + batch_size - 1) // batch_size

    for i in range(0, len(companies), batch_size):
        batch = companies[i:i + batch_size]
        batch_num = i // batch_size + 1
        write_progress(scan_dir, "classifying", "Classifying companies with AI",
                       batch_num, total_batches, started_at, {"matches_so_far": len(all_results)})
        print(f"\nBatch {batch_num}/{total_batches}: Classifying {len(batch)} companies...")

        prompt = f"""You are helping a VC scout startups at a conference.

VC PROFILE / THESIS:
{profile}

The VC wants: STARTUPS (early-stage to growth), NOT mature/established companies, VCs, consulting firms, or associations. Innovative solutions, NOT things that have been around for a while.

Below is a list of {len(batch)} company names attending the conference. For each one, classify it and score its relevance to the VC's thesis.

COMPANIES:
{chr(10).join(f"- {c}" for c in batch)}

Respond with a JSON array. For each company include:
- "name": company name
- "type": "startup" | "growth" | "mature" | "unknown"
- "relevance": 0-10 score (10 = perfect match for the thesis)
- "category": one of: {cat_str}
- "reason": 1-sentence explanation (what they do + why relevant or not)

Only include companies you classify as "startup" or "growth" with relevance >= 5.
Skip mature companies, VCs, associations, consulting firms, etc.

Respond ONLY with the JSON array, no other text. If no companies match, return [].
"""

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
            print(f"  Found {len(matches)} relevant startups in this batch")
        except Exception as e:
            print(f"  ERROR processing batch: {e}")

        if i + batch_size < len(companies):
            time.sleep(1)

    return all_results


# ---------- Step 4: Enrich top candidates ----------

def enrich_one(name, api_key, profile_summary):
    body = {
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 1024,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        "messages": [{"role": "user", "content": f"""Search for "{name}" company. Find:
1. What they do (1-2 sentences)
2. Founded year
3. Funding raised (if any)
4. Stage (seed/A/B/C/growth/public)
5. HQ location
6. Why they're innovative (relevant to: {profile_summary})

Respond as JSON: {{"name": "...", "description": "...", "founded": "...", "funding": "...", "stage": "...", "hq": "...", "innovation": "...", "url": "..."}}
Only JSON, no other text."""}],
    }
    result = call_claude(api_key, body, timeout=90)
    text_parts = [b["text"] for b in result["content"] if b.get("type") == "text"]
    text = " ".join(text_parts)
    return parse_json_text(text)


def enrich_top(candidates, config, scan_dir, started_at, top_n=30):
    api_key = config["api_key"]
    profile_summary = config["profile"][:300]

    top = sorted(candidates, key=lambda x: x.get("relevance", 0), reverse=True)[:top_n]
    print(f"\nDeep-diving top {len(top)} candidates via web search...")

    enriched = []
    for i, company in enumerate(top):
        write_progress(scan_dir, "enriching", "Enriching top candidates via web search",
                       i + 1, len(top), started_at)
        print(f"  [{i+1}/{len(top)}] Searching: {company['name']}...")
        try:
            info = enrich_one(company["name"], api_key, profile_summary)
            enriched.append({**company, **info})
        except Exception as e:
            print(f"    Error: {e}")
            enriched.append(company)
        time.sleep(0.5)

    return enriched


# ---------- Step 5: Auto-generate categories ----------

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

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scout.py <scan_dir>")
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
        write_progress(scan_dir, "error", "No companies found on page", started_at=started_at)
        print("ERROR: no companies extracted from page")
        sys.exit(1)

    # Step 2: Pre-filter
    write_progress(scan_dir, "filtering", "Filtering out known large companies", started_at=started_at)
    candidates = [c for c in all_companies if is_likely_startup(c)]
    print(f"Pre-filtered: {len(all_companies) - len(candidates)} known large/mature removed")
    print(f"Remaining candidates: {len(candidates)}")

    # Step 3: Classify
    relevant = batch_classify(candidates, config, scan_dir, started_at)
    print(f"\nFound {len(relevant)} relevant startups")

    # Step 4: Enrich
    if relevant:
        enriched = enrich_top(relevant, config, scan_dir, started_at)
    else:
        enriched = []

    # Merge enrichment back into the relevant list
    enriched_by_name = {e["name"]: e for e in enriched}
    all_results = [enriched_by_name.get(r["name"], r) for r in relevant]

    # Step 5: Auto-generate categories from the actual dataset (optional)
    if config.get("auto_categorize") and all_results:
        all_results, new_cats = auto_categorize(all_results, config, scan_dir, started_at)
        config["categories"] = new_cats

    output = {
        "total_scraped": len(all_companies),
        "pre_filtered": len(candidates),
        "relevant_matches": len(relevant),
        "top_enriched": len(enriched),
        "results": sorted(all_results, key=lambda x: x.get("relevance", 0), reverse=True),
    }

    with open(os.path.join(scan_dir, "results.json"), "w") as f:
        json.dump(output, f, indent=2)

    # Mark complete in config
    config["completed_at"] = datetime.now(timezone.utc).isoformat()
    # Strip API key from saved config so we don't keep it on disk indefinitely
    config_to_save = {k: v for k, v in config.items() if k != "api_key"}
    with open(config_path, "w") as f:
        json.dump(config_to_save, f, indent=2)

    write_progress(scan_dir, "done", "Complete", started_at=started_at, extra={
        "total_scraped": len(all_companies),
        "relevant_matches": len(relevant),
        "top_enriched": len(enriched),
    })

    print(f"\nDone! Results saved to {scan_dir}/results.json")


if __name__ == "__main__":
    main()
