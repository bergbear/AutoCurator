import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autocurator


def test_weight_item_prefers_more_recent_issue():
    cfg = {
        "weight_stars_exp": 0.35,
        "weight_recency_exp": 0.65,
    }
    now = datetime.utcnow()
    recent = {
        "updated_at": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_stars": 200,
    }
    old = {
        "updated_at": (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_stars": 200,
    }

    assert autocurator.weight_item(recent, cfg) > autocurator.weight_item(old, cfg)


def test_pick_issue_returns_none_for_empty_items():
    store = {"seen": {}, "config": autocurator.DEFAULT_CONFIG}
    assert autocurator.pick_issue([], store) is None


def test_pick_issue_prefers_unseen_candidate():
    issue_seen = {"id": 1, "updated_at": "2026-03-01T00:00:00Z", "_stars": 100}
    issue_unseen = {"id": 2, "updated_at": "2026-03-02T00:00:00Z", "_stars": 100}
    store = {
        "seen": {"1": True},
        "config": {
            "weight_stars_exp": 0.35,
            "weight_recency_exp": 0.65,
        },
    }

    selected = autocurator.pick_issue([issue_seen, issue_unseen], store)
    assert selected["id"] == 2


def test_pick_issue_ignores_skipped_candidates():
    issue_a = {"id": 1, "updated_at": "2026-03-01T00:00:00Z", "_stars": 100}
    issue_b = {"id": 2, "updated_at": "2026-03-02T00:00:00Z", "_stars": 100}
    store = {
        "seen": {},
        "skipped": {"2": True},
        "config": {
            "weight_stars_exp": 0.35,
            "weight_recency_exp": 0.65,
        },
    }

    selected = autocurator.pick_issue([issue_a, issue_b], store)
    assert selected["id"] == 1


def test_build_issue_query_includes_single_label_and_language_filters():
    cfg = {
        "labels": ["good first issue", "help wanted"],
        "languages": ["python", "javascript"],
        "updated_within_days": 30,
    }

    query = autocurator.build_issue_query(
        cfg,
        now_utc=datetime(2026, 3, 4),
        label="good first issue",
        language="python",
    )
    assert 'label:"good first issue"' in query
    assert "language:python" in query
    assert "updated:>=2026-02-02" in query


def test_build_issue_queries_fans_out_label_language_pairs():
    cfg = {
        "labels": ["good first issue", "help wanted"],
        "languages": ["python", "javascript"],
        "updated_within_days": 30,
    }

    queries = autocurator.build_issue_queries(cfg, now_utc=datetime(2026, 3, 4))
    assert len(queries) == 4
    assert any(
        'label:"good first issue"' in q and "language:python" in q for q in queries
    )


def test_parse_config_value_supports_comma_lists_and_json_numbers():
    langs = autocurator.parse_config_value("languages", "python,go")
    stars = autocurator.parse_config_value("min_stars", "10")

    assert langs == ["python", "go"]
    assert stars == 10


def test_score_autotune_result_prefers_more_final_candidates():
    strict_cfg = {"min_stars": 50, "updated_within_days": 30}
    broad_cfg = {"min_stars": 10, "updated_within_days": 180}

    strict_score = autocurator.score_autotune_result(
        {"final": 2, "raw": 20}, strict_cfg
    )
    broad_score = autocurator.score_autotune_result({"final": 5, "raw": 10}, broad_cfg)

    assert broad_score > strict_score


def test_choose_best_autotune_result_returns_highest_scoring_entry():
    r1 = {
        "cfg": {"min_stars": 50, "updated_within_days": 30},
        "stats": {"final": 1, "raw": 100},
    }
    r2 = {
        "cfg": {"min_stars": 10, "updated_within_days": 180},
        "stats": {"final": 3, "raw": 10},
    }

    best = autocurator.choose_best_autotune_result([r1, r2])
    assert best == r2


def test_build_autotune_probe_cfg_caps_breadth_for_speed():
    cfg = {
        "labels": ["l1", "l2", "l3"],
        "languages": ["py", "js", "ts", "go"],
        "page_size": 100,
        "max_pages": 3,
    }

    probe = autocurator.build_autotune_probe_cfg(cfg)
    assert probe["labels"] == ["l1", "l2"]
    assert probe["languages"] == ["py", "js", "ts"]
    assert probe["page_size"] == 20
    assert probe["max_pages"] == 1


def test_get_readme_text_returns_contents_when_file_exists(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Sample\n", encoding="utf-8")

    assert autocurator.get_readme_text(readme) == "# Sample\n"


def test_get_readme_text_returns_none_when_missing(tmp_path):
    missing = tmp_path / "README.md"
    assert autocurator.get_readme_text(missing) is None


def test_cmd_saved_lists_with_issue_ids(monkeypatch, capsys):
    store = {
        "saved": {
            "123": {
                "title": "Fix docs",
                "repo": "octo/repo",
                "url": "https://example.com/123",
            }
        }
    }

    monkeypatch.setattr(autocurator, "load_store", lambda: store)
    monkeypatch.setattr(autocurator, "save_store", lambda _data: None)

    autocurator.cmd_saved(SimpleNamespace(remove=None, clear=False))
    out = capsys.readouterr().out

    assert "- 123: Fix docs [octo/repo] -> https://example.com/123" in out


def test_cmd_saved_remove_deletes_item_and_persists(monkeypatch, capsys):
    store = {
        "saved": {
            "123": {
                "title": "Fix docs",
                "repo": "octo/repo",
                "url": "https://example.com/123",
            }
        }
    }
    writes = []

    monkeypatch.setattr(autocurator, "load_store", lambda: store)
    monkeypatch.setattr(autocurator, "save_store", lambda data: writes.append(data))

    autocurator.cmd_saved(SimpleNamespace(remove="123", clear=False))
    out = capsys.readouterr().out

    assert "Removed saved issue 123" in out
    assert "123" not in store["saved"]
    assert len(writes) == 1


def test_cmd_saved_clear_removes_all_items_and_persists(monkeypatch, capsys):
    store = {
        "saved": {
            "1": {"title": "A", "repo": "r/a", "url": "https://example.com/1"},
            "2": {"title": "B", "repo": "r/b", "url": "https://example.com/2"},
        }
    }
    writes = []

    monkeypatch.setattr(autocurator, "load_store", lambda: store)
    monkeypatch.setattr(autocurator, "save_store", lambda data: writes.append(data))

    autocurator.cmd_saved(SimpleNamespace(remove=None, clear=True))
    out = capsys.readouterr().out

    assert "Cleared 2 saved issue(s)." in out
    assert store["saved"] == {}
    assert len(writes) == 1


def test_cmd_next_skip_fetches_next_candidate(monkeypatch):
    issue_1 = {
        "id": 1,
        "updated_at": "2026-03-01T00:00:00Z",
        "_stars": 100,
        "_repo_full": "octo/repo",
        "title": "First",
        "html_url": "https://example.com/1",
        "labels": [],
    }
    issue_2 = {
        "id": 2,
        "updated_at": "2026-03-02T00:00:00Z",
        "_stars": 100,
        "_repo_full": "octo/repo",
        "title": "Second",
        "html_url": "https://example.com/2",
        "labels": [],
    }
    store = {
        "seen": {},
        "saved": {},
        "skipped": {},
        "config": autocurator.DEFAULT_CONFIG,
    }
    shown_ids = []

    monkeypatch.setattr(autocurator, "load_store", lambda: store)
    monkeypatch.setattr(autocurator, "save_store", lambda _data: None)
    monkeypatch.setattr(autocurator, "get_reference_now_utc", lambda: datetime.utcnow())
    monkeypatch.setattr(
        autocurator,
        "gh_search_issues",
        lambda cfg, now_utc=None: [issue_1, issue_2],
    )
    picks = iter([issue_1, issue_2])
    monkeypatch.setattr(
        autocurator, "pick_issue", lambda items, _store: next(picks, None)
    )

    def fake_interactive_loop(issue, _store):
        shown_ids.append(issue["id"])
        if issue["id"] == 1:
            _store["skipped"]["1"] = True
            return "skip"
        return "quit"

    monkeypatch.setattr(autocurator, "interactive_loop", fake_interactive_loop)

    autocurator.cmd_next(SimpleNamespace())

    assert shown_ids == [1, 2]
