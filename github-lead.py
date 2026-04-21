#!/usr/bin/env python3
"""
GitHub Stargazer Email Scraper
==============================
Scrapes email addresses from stargazers of any GitHub repo.
Built for large repos (20k+ stars) with:
  - Random human-like delays
  - Auto rate-limit detection & pause
  - Checkpoint/resume (crash-safe)
  - Deduplication across multiple repos
  - Clean CSV output ready for cold email tools
  - Robust retry on network errors

Usage:
  python github_lead_scraper.py --token ghp_xxx --repo a2aproject/A2A
  python github_lead_scraper.py --token ghp_xxx --repo a2aproject/A2A,crewAIInc/crewAI
  python github_lead_scraper.py --token ghp_xxx --repo a2aproject/A2A --resume

Author: Built for Raahul / Bindu lead gen
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from requests.exceptions import ConnectionError, Timeout, RequestException
except ImportError:
    print("Installing requests...")
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests
    from requests.exceptions import ConnectionError, Timeout, RequestException


# ─── CONFIG ──────────────────────────────────────────────────────────────────

OUTPUT_DIR = "leads"
CHECKPOINT_DIR = "checkpoints"
NOREPLY_PATTERNS = ["noreply", "users.noreply.github.com", "localhost", "(none)"]
MIN_DELAY = 0.8   # minimum seconds between requests
MAX_DELAY = 2.5   # maximum seconds between requests
BURST_PAUSE_MIN = 5    # after every N users, take a longer break
BURST_PAUSE_MAX = 15
BURST_EVERY = random.randint(30, 60)  # randomized burst interval
RATE_LIMIT_BUFFER = 100  # pause when remaining requests drops below this
REQUEST_TIMEOUT = 30     # seconds to wait for a response
MAX_RETRIES = 5
RETRY_BACKOFF_FACTOR = 1  # seconds (exponential)


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def human_sleep():
    """Sleep for a random human-like duration."""
    delay = random.uniform(MIN_DELAY, MAX_DELAY)
    # occasionally take a slightly longer pause (mimics reading/distraction)
    if random.random() < 0.08:
        delay += random.uniform(2.0, 6.0)
    time.sleep(delay)


def is_valid_email(email: str) -> bool:
    """Filter out GitHub noreply and junk emails."""
    if not email or "@" not in email:
        return False
    email_lower = email.lower().strip()
    for pattern in NOREPLY_PATTERNS:
        if pattern in email_lower:
            return False
    # basic sanity: must have a dot after @
    local, _, domain = email_lower.partition("@")
    if "." not in domain or len(local) == 0:
        return False
    return True


def check_rate_limit(session: requests.Session, headers: dict) -> int:
    """Check remaining API calls. Returns seconds to wait, or 0."""
    try:
        r = safe_get(session, "https://api.github.com/rate_limit", headers=headers)
        if r.status_code == 200:
            core = r.json().get("resources", {}).get("core", {})
            remaining = core.get("remaining", 5000)
            reset_ts = core.get("reset", 0)
            if remaining < RATE_LIMIT_BUFFER:
                wait = max(reset_ts - time.time(), 0) + random.randint(5, 15)
                return int(wait)
    except Exception:
        pass
    return 0


def handle_rate_limit(response: requests.Response, session: requests.Session, headers: dict):
    """If we're close to rate limit, sleep until reset."""
    remaining = int(response.headers.get("X-RateLimit-Remaining", 9999))
    if remaining < RATE_LIMIT_BUFFER:
        reset_ts = int(response.headers.get("X-RateLimit-Reset", 0))
        wait = max(reset_ts - time.time(), 0) + random.randint(5, 15)
        print(f"\n  ⏸  Rate limit low ({remaining} left). Sleeping {int(wait)}s until reset...")
        time.sleep(wait)
        print("  ▶  Resuming!\n")
    elif response.status_code == 403:
        # hard rate limit hit
        wait = check_rate_limit(session, headers)
        if wait > 0:
            print(f"\n  ⏸  403 rate limit. Sleeping {wait}s...")
            time.sleep(wait)
            print("  ▶  Resuming!\n")
        else:
            # secondary rate limit, back off
            backoff = random.randint(60, 120)
            print(f"\n  ⏸  Secondary rate limit. Backing off {backoff}s...")
            time.sleep(backoff)


def safe_get(session: requests.Session, url: str, headers: dict, **kwargs):
    """
    Make a GET request with retries and exponential backoff.
    Handles connection errors, timeouts, and transient HTTP errors.
    """
    retries = 0
    while retries <= MAX_RETRIES:
        try:
            # Use a timeout to prevent hanging
            resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)
            # Retry on 5xx errors and 429 (rate limit) – but rate limit will be handled elsewhere
            if resp.status_code in [429, 500, 502, 503, 504]:
                raise RequestException(f"HTTP {resp.status_code}")
            return resp
        except (ConnectionError, Timeout, RequestException) as e:
            retries += 1
            if retries > MAX_RETRIES:
                raise  # Re-raise after all retries
            wait = RETRY_BACKOFF_FACTOR * (2 ** (retries - 1)) + random.uniform(0, 1)
            print(f"\n  ⚠  Network error on {url.split('/')[-1]}: {e}")
            print(f"  Retry {retries}/{MAX_RETRIES} in {wait:.1f}s...")
            time.sleep(wait)
    # Should never get here, but just in case
    raise RequestException(f"Failed to fetch {url} after {MAX_RETRIES} retries")


def load_checkpoint(repo_slug: str) -> dict:
    """Load checkpoint if it exists."""
    path = Path(CHECKPOINT_DIR) / f"{repo_slug.replace('/', '_')}.json"
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_checkpoint(repo_slug: str, data: dict):
    """Save progress checkpoint."""
    Path(CHECKPOINT_DIR).mkdir(exist_ok=True)
    path = Path(CHECKPOINT_DIR) / f"{repo_slug.replace('/', '_')}.json"
    with open(path, "w") as f:
        json.dump(data, f)


# ─── CORE SCRAPING ───────────────────────────────────────────────────────────

def fetch_all_stargazers(repo: str, session: requests.Session, headers: dict,
                         checkpoint: dict) -> list:
    """Fetch all stargazer usernames from a repo, with pagination."""
    cached = checkpoint.get("stargazers", [])
    start_page = checkpoint.get("stargazers_page", 1)

    if cached and start_page > 1:
        print(f"  📋 Resuming from checkpoint: {len(cached)} stargazers already fetched, page {start_page}")
        stargazers = cached
    else:
        stargazers = []
        start_page = 1

    page = start_page
    consecutive_empty = 0

    while True:
        url = f"https://api.github.com/repos/{repo}/stargazers?per_page=100&page={page}"
        try:
            r = safe_get(session, url, headers)
        except RequestException as e:
            print(f"  ❌ Failed to fetch stargazers page {page}: {e}")
            # Save progress and exit? We'll assume the user will resume later.
            break

        if r.status_code == 403:
            handle_rate_limit(r, session, headers)
            continue  # retry same page

        if r.status_code != 200:
            print(f"  ⚠  Page {page}: HTTP {r.status_code}, skipping")
            consecutive_empty += 1
            if consecutive_empty > 3:
                break
            human_sleep()
            page += 1
            continue

        data = r.json()
        if not data:
            break

        consecutive_empty = 0
        new_users = [u["login"] for u in data if u.get("type") == "User"]
        stargazers.extend(new_users)

        print(f"  ⭐ Page {page}: +{len(new_users)} users (total: {len(stargazers)})")

        # checkpoint every 10 pages
        if page % 10 == 0:
            checkpoint["stargazers"] = stargazers
            checkpoint["stargazers_page"] = page + 1
            save_checkpoint(repo, checkpoint)

        handle_rate_limit(r, session, headers)
        human_sleep()
        page += 1

    # final checkpoint
    checkpoint["stargazers"] = stargazers
    checkpoint["stargazers_page"] = page
    save_checkpoint(repo, checkpoint)

    return stargazers


def extract_email_from_events(username: str, session: requests.Session,
                               headers: dict) -> str:
    """Try to find email from user's public push events (commit history)."""
    url = f"https://api.github.com/users/{username}/events/public?per_page=10"
    try:
        r = safe_get(session, url, headers)
    except RequestException as e:
        print(f"  ⚠  Failed to fetch events for {username}: {e}")
        return ""

    if r.status_code != 200:
        handle_rate_limit(r, session, headers)
        return ""

    handle_rate_limit(r, session, headers)

    try:
        events = r.json()
    except Exception:
        return ""

    for event in events:
        if event.get("type") == "PushEvent":
            commits = event.get("payload", {}).get("commits", [])
            for commit in commits:
                email = commit.get("author", {}).get("email", "")
                if is_valid_email(email):
                    return email
    return ""


def scrape_user_profile(username: str, session: requests.Session,
                         headers: dict) -> dict:
    """Fetch profile + try to find email via profile and events."""
    # 1. Get profile
    try:
        r = safe_get(session, f"https://api.github.com/users/{username}", headers)
    except RequestException as e:
        print(f"  ⚠  Failed to fetch profile for {username}: {e}")
        return None

    if r.status_code == 403:
        handle_rate_limit(r, session, headers)
        # retry after handling rate limit
        try:
            r = safe_get(session, f"https://api.github.com/users/{username}", headers)
        except RequestException:
            return None

    if r.status_code != 200:
        return None

    handle_rate_limit(r, session, headers)
    profile = r.json()
    email = profile.get("email", "") or ""

    # 2. If no public email, check commit events
    if not is_valid_email(email):
        human_sleep()
        email = extract_email_from_events(username, session, headers)

    return {
        "username": username,
        "name": profile.get("name", "") or "",
        "email": email if is_valid_email(email) else "",
        "company": profile.get("company", "") or "",
        "location": profile.get("location", "") or "",
        "bio": (profile.get("bio", "") or "").replace("\n", " ").replace("\r", ""),
        "twitter": profile.get("twitter_username", "") or "",
        "followers": profile.get("followers", 0),
        "public_repos": profile.get("public_repos", 0),
        "profile_url": profile.get("html_url", ""),
    }


def scrape_repo(repo: str, token: str, resume: bool = False,
                seen_users: set = None) -> list:
    """Main scraping loop for a single repo."""
    global BURST_EVERY
    
    print(f"\n{'='*60}")
    print(f"  🎯 Target: {repo}")
    print(f"{'='*60}\n")

    session = requests.Session()
    # randomize user agent slightly
    agents = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Mozilla/5.0 (X11; Linux x86_64)",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    ]
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": random.choice(agents),
    }

    if seen_users is None:
        seen_users = set()

    # load checkpoint
    checkpoint = load_checkpoint(repo) if resume else {}

    # ─── Phase 1: Fetch stargazers ───
    print("Phase 1: Fetching stargazers...\n")
    stargazers = fetch_all_stargazers(repo, session, headers, checkpoint)
    print(f"\n  Total stargazers: {len(stargazers)}")

    # deduplicate against already-scraped users
    new_users = [u for u in stargazers if u not in seen_users]
    skipped = len(stargazers) - len(new_users)
    if skipped > 0:
        print(f"  Skipping {skipped} already-scraped users")
    print(f"  Users to scrape: {len(new_users)}\n")

    # ─── Phase 2: Scrape profiles ───
    print("Phase 2: Scraping profiles & emails...\n")
    results = checkpoint.get("results", []) if resume else []
    scraped_set = {r["username"] for r in results}
    remaining_users = [u for u in new_users if u not in scraped_set]

    emails_found = sum(1 for r in results if r.get("email"))
    burst_counter = 0
    start_time = time.time()

    for i, username in enumerate(remaining_users):
        # progress
        total_done = len(results) + 1
        elapsed = time.time() - start_time
        rate = (i + 1) / elapsed if elapsed > 0 else 0
        eta = (len(remaining_users) - i - 1) / rate if rate > 0 else 0

        try:
            profile = scrape_user_profile(username, session, headers)
            if profile is None:
                print(f"  [{total_done}/{len(new_users)}] {username} → ⚠ failed")
                human_sleep()
                continue

            results.append(profile)
            seen_users.add(username)

            if profile["email"]:
                emails_found += 1
                print(f"  [{total_done}/{len(new_users)}] {username} → ✅ {profile['email']}")
            else:
                print(f"  [{total_done}/{len(new_users)}] {username} → ❌ no email")
        except Exception as e:
            print(f"  [{total_done}/{len(new_users)}] {username} → 💥 unexpected error: {e}")
            # Still continue to next user

        # show stats periodically
        if total_done % 50 == 0:
            hit_rate = (emails_found / total_done) * 100
            print(f"\n  📊 Progress: {total_done}/{len(new_users)} | "
                  f"Emails: {emails_found} ({hit_rate:.1f}%) | "
                  f"ETA: {int(eta/60)}m {int(eta%60)}s\n")

        # checkpoint every 25 users
        if total_done % 25 == 0:
            checkpoint["results"] = results
            save_checkpoint(repo, checkpoint)

        # burst pause — take a longer break every N users
        burst_counter += 1
        if burst_counter >= BURST_EVERY:
            pause = random.uniform(BURST_PAUSE_MIN, BURST_PAUSE_MAX)
            print(f"  💤 Human pause: {pause:.0f}s...")
            time.sleep(pause)
            burst_counter = 0
            # re-randomize next burst interval
            BURST_EVERY = random.randint(25, 55)

        human_sleep()

    # final checkpoint
    checkpoint["results"] = results
    save_checkpoint(repo, checkpoint)

    return results


# ─── OUTPUT ──────────────────────────────────────────────────────────────────

def save_csv(results: list, filename: str):
    """Save results to CSV."""
    if not results:
        print("No results to save.")
        return

    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    filepath = Path(OUTPUT_DIR) / filename

    fieldnames = ["username", "name", "email", "company", "location",
                  "bio", "twitter", "followers", "public_repos", "profile_url"]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    return filepath


def print_summary(results: list, repo: str):
    """Print final stats."""
    total = len(results)
    with_email = [r for r in results if r["email"]]
    hit_rate = (len(with_email) / total * 100) if total > 0 else 0

    print(f"\n{'='*60}")
    print(f"  📊 RESULTS: {repo}")
    print(f"{'='*60}")
    print(f"  Total profiles scraped : {total}")
    print(f"  Emails found           : {len(with_email)}")
    print(f"  Hit rate               : {hit_rate:.1f}%")
    print(f"  No email               : {total - len(with_email)}")
    if with_email:
        print(f"\n  Top leads (by followers):")
        top = sorted(with_email, key=lambda x: x["followers"], reverse=True)[:10]
        for u in top:
            company = f" @ {u['company']}" if u["company"] else ""
            print(f"    {u['name'] or u['username']}{company} — {u['email']} ({u['followers']} followers)")
    print(f"{'='*60}\n")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape emails from GitHub repo stargazers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single repo
  python github_lead_scraper.py --token ghp_xxx --repo a2aproject/A2A

  # Multiple repos (comma-separated)
  python github_lead_scraper.py --token ghp_xxx --repo a2aproject/A2A,crewAIInc/crewAI

  # Resume after crash
  python github_lead_scraper.py --token ghp_xxx --repo a2aproject/A2A --resume

  # Only export users with emails
  python github_lead_scraper.py --token ghp_xxx --repo a2aproject/A2A --emails-only
        """
    )
    parser.add_argument("--token", required=True, help="GitHub personal access token (ghp_...)")
    parser.add_argument("--repo", required=True, help="Repo(s) in owner/name format, comma-separated")
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint")
    parser.add_argument("--emails-only", action="store_true", help="Only save users with emails")

    args = parser.parse_args()

    # validate token
    print("\n🔑 Validating token...")
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": f"token {args.token}"},
                         timeout=10)
    except Exception as e:
        print(f"❌ Network error validating token: {e}")
        sys.exit(1)

    if r.status_code != 200:
        print(f"❌ Token invalid (HTTP {r.status_code}). Get one at github.com/settings/tokens")
        sys.exit(1)
    user = r.json()
    print(f"✅ Authenticated as: {user.get('login', 'unknown')}\n")

    # check rate limit
    try:
        r2 = requests.get("https://api.github.com/rate_limit",
                          headers={"Authorization": f"token {args.token}"},
                          timeout=10)
        if r2.status_code == 200:
            remaining = r2.json().get("resources", {}).get("core", {}).get("remaining", "?")
            print(f"📊 API calls remaining: {remaining}/5000\n")
    except Exception:
        print("⚠  Could not check rate limit\n")

    # scrape each repo
    repos = [r.strip() for r in args.repo.split(",")]
    all_results = []
    seen_users = set()

    for repo in repos:
        results = scrape_repo(repo, args.token, resume=args.resume, seen_users=seen_users)
        all_results.extend(results)
        print_summary(results, repo)

    # deduplicate across repos
    unique = {}
    for r in all_results:
        if r["username"] not in unique:
            unique[r["username"]] = r
    all_results = list(unique.values())

    # filter if emails-only
    if args.emails_only:
        all_results = [r for r in all_results if r["email"]]

    # save combined CSV
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    repo_label = repos[0].split("/")[1] if len(repos) == 1 else "combined"
    filename = f"{repo_label}_leads_{timestamp}.csv"
    filepath = save_csv(all_results, filename)

    # also save emails-only version
    email_results = [r for r in all_results if r["email"]]
    if email_results:
        email_filename = f"{repo_label}_emails_only_{timestamp}.csv"
        email_filepath = save_csv(email_results, email_filename)

    print(f"\n✅ DONE!")
    print(f"   All leads  : {filepath}  ({len(all_results)} users)")
    if email_results:
        print(f"   Emails only: {Path(OUTPUT_DIR) / email_filename}  ({len(email_results)} emails)")
    print(f"\n   Ready to import into Instantly! 🚀\n")


if __name__ == "__main__":
    main()