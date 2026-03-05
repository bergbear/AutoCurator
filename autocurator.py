#!/usr/bin/env python3
import argparse
import json
import os
import random
import sys
import textwrap
import time
import webbrowser
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

STORE = Path.home() / ".autocurator.json"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
README_PATH = Path(__file__).resolve().with_name("README.md")

DEFAULT_CONFIG = {
    "labels": ["good first issue", "help wanted", "documentation"],
    "languages": ["python", "javascript", "typescript", "go"],  # edit me
    "min_stars": 10,  # repo popularity floor
    "updated_within_days": 90,  # freshness
    "exclude_terms": ["translation", "typo", "readme"],  # noise
    "page_size": 50,  # search page size (max 100)
    "max_pages": 2,  # how many pages to sample
    "weight_stars_exp": 0.35,
    "weight_recency_exp": 0.65,
}

HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"


class GitHubRateLimitError(RuntimeError):
    pass


def load_store():
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except Exception:
            pass
    return {"seen": {}, "saved": {}, "skipped": {}, "config": DEFAULT_CONFIG}


def save_store(data):
    STORE.write_text(json.dumps(data, indent=2))


def get_reference_now_utc():
    """Best-effort GitHub server time to avoid local clock skew issues."""
    try:
        resp = requests.get(
            "https://api.github.com/rate_limit", headers=HEADERS, timeout=10
        )
        date_header = resp.headers.get("Date")
        if date_header:
            dt = parsedate_to_datetime(date_header)
            return dt.replace(tzinfo=None)
    except requests.RequestException:
        pass
    return datetime.utcnow()


def normalize_query_terms(raw_terms):
    if not raw_terms:
        return []
    terms = []
    for raw in raw_terms:
        for part in str(raw).split(","):
            term = part.strip()
            if term and term not in terms:
                terms.append(term)
    return terms


def raise_for_github_rate_limit(response):
    is_rate_limited = response.status_code == 403 and (
        "rate limit" in response.text.lower()
        or response.headers.get("X-RateLimit-Remaining") == "0"
    )
    if not is_rate_limited:
        return

    reset_at = "unknown"
    reset_ts = response.headers.get("X-RateLimit-Reset")
    if reset_ts and str(reset_ts).isdigit():
        reset_at = datetime.utcfromtimestamp(int(reset_ts)).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )

    if GITHUB_TOKEN:
        raise GitHubRateLimitError(
            f"Rate limited by GitHub. Try again after {reset_at} or reduce page_size/max_pages."
        )

    raise GitHubRateLimitError(
        f"Rate limited by GitHub. Add GITHUB_TOKEN in .env and run `autocurator.py auth` (reset: {reset_at})."
    )


def build_issue_query(cfg, now_utc=None, label=None, language=None, query_terms=None):
    now_utc = now_utc or datetime.utcnow()
    q_parts = []
    # Type & state
    q_parts += ["is:issue", "is:open", "no:assignee"]
    if label:
        q_parts.append(f'label:"{label}"')
    if language:
        q_parts.append(f"language:{language}")
    for term in query_terms or []:
        q_parts.append(f'"{term}"' if any(ch.isspace() for ch in term) else term)
    # Freshness
    since = (now_utc - timedelta(days=cfg["updated_within_days"])).date().isoformat()
    q_parts.append(f"updated:>={since}")
    return " ".join(q_parts)


def build_issue_queries(cfg, now_utc=None, query_terms=None):
    labels = cfg.get("labels") or [None]
    languages = cfg.get("languages") or [None]
    queries = []
    for label in labels:
        for language in languages:
            queries.append(
                build_issue_query(
                    cfg,
                    now_utc=now_utc,
                    label=label,
                    language=language,
                    query_terms=query_terms,
                )
            )
    return queries


def filter_items(items, cfg):
    def drop(it):
        if it.get("_archived"):
            return True
        assignees = it.get("assignees") or []
        if it.get("assignee") or assignees:
            return True
        if it.get("_stars", 0) < cfg["min_stars"]:
            return True
        title = (it.get("title") or "").lower()
        if any(term.lower() in title for term in cfg["exclude_terms"]):
            return True
        return False

    return [it for it in items if not drop(it)]


def gh_search_issues(cfg, now_utc=None, query_terms=None):
    base = "https://api.github.com/search/issues"
    queries = build_issue_queries(cfg, now_utc=now_utc, query_terms=query_terms)
    # GitHub issue search does not behave well with OR+language; fan out queries and union results.
    all_items = []
    seen_issue_ids = set()
    seen_repo = {}
    for query in queries:
        params = {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": cfg["page_size"],
            "page": 1,
        }
        for page in range(1, cfg["max_pages"] + 1):
            params["page"] = page
            r = requests.get(base, headers=HEADERS, params=params, timeout=20)
            raise_for_github_rate_limit(r)
            r.raise_for_status()
            data = r.json()
            items = data.get("items", [])
            if not items:
                break
            # Enrich with repo stars
            for it in items:
                if it.get("id") in seen_issue_ids:
                    continue
                seen_issue_ids.add(it.get("id"))
                repo_full = it["repository_url"].split("repos/")[-1]
                if repo_full not in seen_repo:
                    repo_resp = requests.get(
                        it["repository_url"], headers=HEADERS, timeout=15
                    )
                    if repo_resp.ok:
                        repo = repo_resp.json()
                        seen_repo[repo_full] = {
                            "stars": repo.get("stargazers_count", 0),
                            "archived": repo.get("archived", False),
                        }
                    else:
                        seen_repo[repo_full] = {"stars": 0, "archived": False}
                meta = seen_repo[repo_full]
                it["_repo_full"] = repo_full
                it["_stars"] = meta["stars"]
                it["_archived"] = meta["archived"]
                all_items.append(it)
            # Be polite
            time.sleep(0.2)

    all_items = filter_items(all_items, cfg)
    return all_items


def gh_search_issues_with_stats(cfg, now_utc=None, query_terms=None):
    base = "https://api.github.com/search/issues"
    queries = build_issue_queries(cfg, now_utc=now_utc, query_terms=query_terms)
    all_items = []
    seen_issue_ids = set()
    seen_repo = {}
    for query in queries:
        params = {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": cfg["page_size"],
            "page": 1,
        }
        for page in range(1, cfg["max_pages"] + 1):
            params["page"] = page
            r = requests.get(base, headers=HEADERS, params=params, timeout=20)
            raise_for_github_rate_limit(r)
            r.raise_for_status()
            data = r.json()
            items = data.get("items", [])
            if not items:
                break
            for it in items:
                if it.get("id") in seen_issue_ids:
                    continue
                seen_issue_ids.add(it.get("id"))
                repo_full = it["repository_url"].split("repos/")[-1]
                if repo_full not in seen_repo:
                    repo_resp = requests.get(
                        it["repository_url"], headers=HEADERS, timeout=15
                    )
                    if repo_resp.ok:
                        repo = repo_resp.json()
                        seen_repo[repo_full] = {
                            "stars": repo.get("stargazers_count", 0),
                            "archived": repo.get("archived", False),
                        }
                    else:
                        seen_repo[repo_full] = {"stars": 0, "archived": False}
                meta = seen_repo[repo_full]
                it["_repo_full"] = repo_full
                it["_stars"] = meta["stars"]
                it["_archived"] = meta["archived"]
                all_items.append(it)
            time.sleep(0.2)

    archived_drop = [it for it in all_items if it.get("_archived")]
    stars_drop = [
        it
        for it in all_items
        if not it.get("_archived") and it.get("_stars", 0) < cfg["min_stars"]
    ]
    exclude_drop = [
        it
        for it in all_items
        if (not it.get("_archived"))
        and it.get("_stars", 0) >= cfg["min_stars"]
        and any(
            term.lower() in (it.get("title") or "").lower()
            for term in cfg["exclude_terms"]
        )
    ]

    filtered = filter_items(all_items, cfg)
    stats = {
        "queries": queries,
        "raw": len(all_items),
        "archived_drop": len(archived_drop),
        "stars_drop": len(stars_drop),
        "exclude_drop": len(exclude_drop),
        "final": len(filtered),
    }
    return filtered, stats


def parse_config_value(key, raw):
    list_keys = {"labels", "languages", "exclude_terms"}
    if key in list_keys:
        if raw.strip().startswith("["):
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError(f"{key} must be a list")
            return parsed
        # Allow quick comma-separated CLI input.
        return [p.strip() for p in raw.split(",") if p.strip()]

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def build_autotune_configs(base_cfg):
    """Return a small ladder of configs from strict to broad."""
    base_labels = base_cfg.get("labels") or DEFAULT_CONFIG["labels"]
    base_languages = base_cfg.get("languages") or DEFAULT_CONFIG["languages"]
    return [
        {
            **base_cfg,
            "labels": base_labels,
            "languages": base_languages,
            "updated_within_days": 30,
            "min_stars": 50,
        },
        {
            **base_cfg,
            "labels": base_labels,
            "languages": base_languages,
            "updated_within_days": 90,
            "min_stars": 25,
        },
        {
            **base_cfg,
            "labels": base_labels,
            "languages": list(dict.fromkeys(base_languages + ["go"])),
            "updated_within_days": 180,
            "min_stars": 10,
        },
        {
            **base_cfg,
            "labels": list(dict.fromkeys(base_labels + ["documentation"])),
            "languages": list(dict.fromkeys(base_languages + ["go", "java", "rust"])),
            "updated_within_days": 365,
            "min_stars": 0,
        },
    ]


def score_autotune_result(stats, cfg):
    """Higher is better: prioritize viable final pool, then raw signal, then quality knobs."""
    return (
        (stats.get("final", 0) * 1000)
        + stats.get("raw", 0)
        + cfg.get("min_stars", 0)
        - cfg.get("updated_within_days", 0)
    )


def choose_best_autotune_result(results):
    if not results:
        return None
    return max(results, key=lambda r: score_autotune_result(r["stats"], r["cfg"]))


def build_autotune_probe_cfg(cfg):
    """Limit probe breadth so autotune remains responsive."""
    probe = dict(cfg)
    probe["page_size"] = min(int(probe.get("page_size", 50)), 20)
    probe["max_pages"] = min(int(probe.get("max_pages", 2)), 1)
    probe["labels"] = (probe.get("labels") or [])[:2]
    probe["languages"] = (probe.get("languages") or [])[:3]
    return probe


def weight_item(it, cfg):
    # Recency weight: newer updated_at => higher weight
    upd = datetime.strptime(it["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
    days = max((datetime.utcnow() - upd).total_seconds() / 86400.0, 0.1)
    recency = 1.0 / (days ** cfg["weight_recency_exp"])
    stars = max(it.get("_stars", 1), 1)
    stars_w = stars ** cfg["weight_stars_exp"]
    return recency * stars_w


def pick_issue(items, store):
    seen = store["seen"]
    skipped = store.get("skipped", {})
    # Prefer unseen and not explicitly skipped.
    candidates = [
        it for it in items if str(it["id"]) not in seen and str(it["id"]) not in skipped
    ]
    if not candidates:
        candidates = [it for it in items if str(it["id"]) not in skipped]
    if not candidates:
        return None
    weights = [
        weight_item(it, store.get("config", DEFAULT_CONFIG)) for it in candidates
    ]
    total = sum(weights)
    probs = [w / total for w in weights] if total > 0 else None
    return random.choices(candidates, weights=probs, k=1)[0]


def fmt_issue(it):
    repo = it["_repo_full"]
    title = it["title"].strip()
    url = it["html_url"]
    labels = [label["name"] for label in it.get("labels", [])]
    stars = it.get("_stars", 0)
    updated = it["updated_at"].replace("T", " ").replace("Z", " UTC")
    body = it.get("body") or ""
    body = textwrap.shorten(" ".join(body.split()), width=220, placeholder="…")
    return textwrap.dedent(
        f"""
    ── 🎯 Auto-Curator pick ─────────────────────────────
    Repo     : {repo}  ⭐ {stars}
    Title    : {title}
    Labels   : {", ".join(labels) if labels else "—"}
    Updated  : {updated}
    Link     : {url}
    Summary  : {body}
    Actions  : [o] open  [s] save  [k] skip  [q] quit
    """
    ).strip()


def interactive_loop(issue, store):
    print(fmt_issue(issue))
    while True:
        try:
            choice = input("> ").strip().lower()
        except KeyboardInterrupt:
            print("\nInterrupted. Returning to shell.")
            return "quit"
        if choice in ("o", "open"):
            webbrowser.open(issue["html_url"])
        elif choice in ("s", "save"):
            store["saved"][str(issue["id"])] = {
                "url": issue["html_url"],
                "title": issue["title"],
                "repo": issue["_repo_full"],
                "at": datetime.utcnow().isoformat() + "Z",
            }
            print("✓ saved")
        elif choice in ("k", "skip"):
            store["skipped"][str(issue["id"])] = True
            print("skipped")
            return "skip"
        elif choice in ("q", "quit", "exit"):
            return "quit"
        else:
            print("Commands: o=open  s=save  k=skip  q=quit")


def cmd_next(args):
    store = load_store()
    cfg = store.get("config", DEFAULT_CONFIG)
    query_terms = normalize_query_terms(getattr(args, "query", None))
    now_utc = get_reference_now_utc()
    queries = build_issue_queries(cfg, now_utc=now_utc, query_terms=query_terms)
    print(f"Search queries ({len(queries)}):")
    for q in queries:
        print(f"  {q}")
    try:
        items = gh_search_issues(cfg, now_utc=now_utc, query_terms=query_terms)
    except GitHubRateLimitError as exc:
        print(exc)
        return
    if not items:
        print("No candidates found. Try widening filters (languages, min_stars).")
        return

    while True:
        it = pick_issue(items, store)
        if not it:
            print("No more candidates available right now.")
            break

        store["seen"][str(it["id"])] = True
        save_store(store)

        action = interactive_loop(it, store)
        save_store(store)

        if action == "skip":
            continue
        break


def cmd_config(args):
    store = load_store()
    if args.reset:
        store["config"] = DEFAULT_CONFIG
        save_store(store)
        print("Config reset to defaults.")
        return
    if args.set:
        cfg = dict(store.get("config", DEFAULT_CONFIG))
        for key, raw in args.set:
            if key not in DEFAULT_CONFIG:
                print(
                    f"Unknown config key: {key}. Available keys: {', '.join(DEFAULT_CONFIG.keys())}"
                )
                return
            cfg[key] = parse_config_value(key, raw)
        store["config"] = cfg
        save_store(store)
        print("Config updated.")
    # print current
    print(json.dumps(store.get("config", DEFAULT_CONFIG), indent=2))


def cmd_diagnose(args):
    store = load_store()
    cfg = store.get("config", DEFAULT_CONFIG)
    now_utc = get_reference_now_utc()
    try:
        _, stats = gh_search_issues_with_stats(cfg, now_utc=now_utc)
    except GitHubRateLimitError as exc:
        print(exc)
        return
    print(f"Queries tested: {len(stats['queries'])}")
    for q in stats["queries"][:3]:
        print(f"  {q}")
    if len(stats["queries"]) > 3:
        print("  ...")
    print("Counts:")
    print(f"  raw fetched   : {stats['raw']}")
    print(f"  archived drop : {stats['archived_drop']}")
    print(f"  stars drop    : {stats['stars_drop']}")
    print(f"  title drop    : {stats['exclude_drop']}")
    print(f"  final         : {stats['final']}")

    if stats["final"] > 0:
        print("Diagnosis: current config can produce candidates.")
        return

    print("Diagnosis: no candidates after filtering.")
    if stats["raw"] == 0:
        print(
            "Suggestion: widen upstream search (labels/languages/updated_within_days/page_size/max_pages)."
        )
    if stats["stars_drop"] >= max(stats["raw"] // 2, 1):
        print(
            "Suggestion: lower min_stars (example: autocurator.py config --set min_stars 10)."
        )
    if stats["exclude_drop"] >= max(stats["raw"] // 3, 1):
        print(
            'Suggestion: relax exclude_terms (example: autocurator.py config --set exclude_terms "translation").'
        )
    print(
        'Suggestion: broader starter preset -> autocurator.py config --set labels "good first issue,help wanted,documentation" --set languages "python,javascript,typescript,go" --set updated_within_days 90 --set min_stars 10'
    )


def cmd_autotune(args):
    store = load_store()
    base_cfg = dict(store.get("config", DEFAULT_CONFIG))
    now_utc = get_reference_now_utc()

    candidates = build_autotune_configs(base_cfg)
    results = []

    print(f"Autotune: evaluating {len(candidates)} candidate configs...")
    for idx, cfg in enumerate(candidates, start=1):
        probe_cfg = build_autotune_probe_cfg(cfg)
        try:
            _, stats = gh_search_issues_with_stats(probe_cfg, now_utc=now_utc)
        except GitHubRateLimitError as exc:
            print(exc)
            return
        results.append({"cfg": cfg, "stats": stats})
        print(
            f"  [{idx}] final={stats['final']:>4} raw={stats['raw']:>4} "
            f"stars>={cfg['min_stars']:<3} days={cfg['updated_within_days']:<3} "
            f"labels={len(cfg.get('labels', []))} langs={len(cfg.get('languages', []))}"
        )

    best = choose_best_autotune_result(results)
    if not best:
        print("Autotune failed to produce a result.")
        return

    best_cfg = best["cfg"]
    best_stats = best["stats"]
    print("Best candidate:")
    print(
        f"  final={best_stats['final']} raw={best_stats['raw']} "
        f"stars>={best_cfg['min_stars']} days={best_cfg['updated_within_days']}"
    )

    if args.dry_run:
        print("Dry run: no config changes were saved.")
        print(json.dumps(best_cfg, indent=2))
        return

    store["config"] = best_cfg
    save_store(store)
    print("Autotune applied. Current config:")
    print(json.dumps(best_cfg, indent=2))


def cmd_saved(args):
    store = load_store()
    saved = store.get("saved", {})

    if args.remove:
        issue_id = str(args.remove)
        if issue_id not in saved:
            print(f"Saved issue not found: {issue_id}")
            return
        removed = saved.pop(issue_id)
        save_store(store)
        print(f"Removed saved issue {issue_id}: {removed['title']}")
        return

    if args.clear:
        if not saved:
            print("No saved issues.")
            return
        count = len(saved)
        saved.clear()
        save_store(store)
        print(f"Cleared {count} saved issue(s).")
        return

    if not saved:
        print("No saved issues.")
        return
    for k, v in saved.items():
        print(f"- {k}: {v['title']} [{v['repo']}] -> {v['url']}")


def cmd_auth(args):
    print("GitHub token loaded:", "yes" if GITHUB_TOKEN else "no")
    try:
        rate_resp = requests.get(
            "https://api.github.com/rate_limit", headers=HEADERS, timeout=15
        )
        rate_resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"Could not fetch rate limit info: {exc}")
        return

    core = rate_resp.json().get("resources", {}).get("core", {})
    remaining = core.get("remaining", "?")
    limit = core.get("limit", "?")
    reset_ts = core.get("reset")
    reset_at = (
        datetime.utcfromtimestamp(reset_ts).strftime("%Y-%m-%d %H:%M:%S UTC")
        if isinstance(reset_ts, (int, float))
        else "unknown"
    )

    print(f"Core rate limit: {remaining}/{limit}")
    print(f"Resets at      : {reset_at}")

    if not GITHUB_TOKEN:
        print("Auth status    : unauthenticated (set GITHUB_TOKEN in .env)")
        return

    try:
        user_resp = requests.get(
            "https://api.github.com/user", headers=HEADERS, timeout=15
        )
        if user_resp.ok:
            login = user_resp.json().get("login", "unknown")
            print(f"Auth status    : authenticated as {login}")
        elif user_resp.status_code == 401:
            print("Auth status    : token invalid (401)")
        else:
            print(
                f"Auth status    : token present, user check returned {user_resp.status_code}"
            )
    except requests.RequestException as exc:
        print(f"Auth status    : token present, but user check failed: {exc}")


def get_readme_text(path=README_PATH):
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def cmd_readme(args):
    text = get_readme_text()
    if text is None:
        print("README.md not found in project root.")
        return
    print(text)


def main():
    ap = argparse.ArgumentParser(
        description="Auto-Curator: fetch a great GitHub issue to fix."
    )
    sub = ap.add_subparsers(dest="cmd")

    n = sub.add_parser("next", help="fetch & show a curated issue (interactive)")
    n.add_argument(
        "--query",
        action="append",
        metavar="KEYWORD",
        help="keyword to include in issue search, repeatable",
    )
    n.set_defaults(func=cmd_next)
    s = sub.add_parser("saved", help="list/remove/clear saved issues")
    mut = s.add_mutually_exclusive_group()
    mut.add_argument(
        "--remove",
        "--rm",
        metavar="ISSUE_ID",
        dest="remove",
        help="remove one saved issue",
    )
    mut.add_argument("--clear", action="store_true", help="remove all saved issues")
    s.set_defaults(func=cmd_saved)
    sub.add_parser("readme", help="display the project README").set_defaults(
        func=cmd_readme
    )
    sub.add_parser(
        "auth", help="show GitHub token/auth and rate-limit status"
    ).set_defaults(func=cmd_auth)
    sub.add_parser(
        "diagnose", help="show search/filter funnel and tuning suggestions"
    ).set_defaults(func=cmd_diagnose)
    t = sub.add_parser(
        "autotune", help="try multiple presets and apply the best candidate config"
    )
    t.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate candidates but do not save any config changes",
    )
    t.set_defaults(func=cmd_autotune)
    c = sub.add_parser("config", help="show (or reset) config")
    c.add_argument("--reset", action="store_true")
    c.add_argument(
        "--set",
        action="append",
        nargs=2,
        metavar=("KEY", "VALUE"),
        help='set config key/value, repeatable (ex: --set min_stars 10 or --set languages "python,go")',
    )
    c.set_defaults(func=cmd_config)

    if len(sys.argv) == 1:
        ap.print_help()
        sys.exit(0)
    args = ap.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Exiting cleanly.")
        sys.exit(130)


if __name__ == "__main__":
    main()
