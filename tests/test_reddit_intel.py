"""Unit tests for bin/reddit-intel. No network."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout

import http.client

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "reddit-intel"

# The CLI has no .py suffix, so the default suffix-based spec lookup returns None.
_loader = importlib.machinery.SourceFileLoader("reddit_intel", str(SCRIPT))
spec = importlib.util.spec_from_loader("reddit_intel", _loader)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
# dataclass() looks the class's module up in sys.modules while the file executes.
sys.modules["reddit_intel"] = mod
spec.loader.exec_module(mod)


def raw_post(pid, created, title="Hello", selftext="body", score=1, num_comments=0, sub="stocks", **extra):
    row = {
        "id": pid,
        "title": title,
        "selftext": selftext,
        "created_utc": created,
        "score": score,
        "num_comments": num_comments,
        "subreddit": sub,
    }
    row.update(extra)
    return row


class ScriptedHttp:
    """Mock for mod._http_get: script of (status, headers, body) or Exception."""
    def __init__(self, script):
        self.script = list(script)
        self.urls = []

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        if not self.script:
            raise AssertionError(f"unexpected request {url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        status, hdrs, body = item
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        return status, dict(hdrs or {}), body


def http_reply(code, body, headers=None):
    return (code, headers or {}, body)


class RedditIntelTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "pages": mod.MAX_WINDOW_POSTS,
            "sleep": mod.time.sleep,
            "uniform": mod.random.uniform,
            "pacer": mod._PACER,
            "http_get": mod._http_get,
            "search": mod.ArcticShiftBackend.search_window,
            "count": mod.ArcticShiftBackend.count_comments,
            "cache_env": os.environ.get("REDDIT_INTEL_CACHE"),
        }
        # Tests must never wait on the rate limiter.
        mod._PACER = mod.AdaptivePacer(rate=10000, capacity=10000)
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["REDDIT_INTEL_CACHE"] = os.path.join(self._tmpdir.name, "cache.json")
        self.slept = []
        mod.time.sleep = lambda seconds: self.slept.append(seconds)
        mod.random.uniform = lambda lo, hi: hi

    def tearDown(self):
        mod.MAX_WINDOW_POSTS = self._saved["pages"]
        mod.time.sleep = self._saved["sleep"]
        mod.random.uniform = self._saved["uniform"]
        mod._PACER = self._saved["pacer"]
        mod._http_get = self._saved["http_get"]
        mod.ArcticShiftBackend.search_window = self._saved["search"]
        mod.ArcticShiftBackend.count_comments = self._saved["count"]
        if self._saved["cache_env"] is None:
            os.environ.pop("REDDIT_INTEL_CACHE", None)
        else:
            os.environ["REDDIT_INTEL_CACHE"] = self._saved["cache_env"]
        self._tmpdir.cleanup()

    def test_format_epoch_is_integer_seconds(self):
        # Fractional seconds are a live HTTP 400 from Arctic Shift.
        self.assertEqual(mod.format_epoch(1_700_000_000.9), "1700000000")
        self.assertEqual(mod.format_epoch(1_700_000_000.0), "1700000000")
        self.assertEqual(mod.format_epoch(-5), "0")

    def test_parse_since_and_subs(self):
        now = 1_000_000.0
        self.assertEqual(mod.parse_since("24h", now), now - 86400)
        self.assertEqual(mod.parse_since("7d", now), now - 7 * 86400)
        self.assertEqual(mod.parse_since("2020-01-02", now), 1577923200.0)
        with self.assertRaises(ValueError):
            mod.parse_since("1w", now)
        self.assertEqual(mod.parse_subs("r/Stocks, /r/stocks, investing"), ["Stocks", "investing"])
        for bad in (None, "", "all", "*", "r/all", "popular", "has space"):
            with self.assertRaises(ValueError):
                mod.parse_subs(bad)

    def test_usage_exits_1_and_subs_required_before_network(self):
        def explode(*args, **kwargs):
            raise AssertionError("network used")

        mod.urllib.request.urlopen = explode
        with self.assertRaises(SystemExit) as ctx:
            mod.main([])
        self.assertEqual(ctx.exception.code, 1)
        with self.assertRaises(SystemExit) as ctx:
            mod.main(["search"])
        self.assertEqual(ctx.exception.code, 1)
        err = io.StringIO()
        with redirect_stderr(err):
            code = mod.main(["search", "nvda", "--since", "24h"])
        self.assertEqual(code, 1)
        text = err.getvalue()
        self.assertIn("--subs", text)
        self.assertIn("subreddit=all", text)
        err = io.StringIO()
        with redirect_stderr(err):
            code = mod.main(["digest", "nvda", "--subs", "all"])
        self.assertEqual(code, 1)
        self.assertIn("--subs", err.getvalue())

    def test_engagement_flag_defaults(self):
        parser = mod.build_parser()
        plain = parser.parse_args(["search", "q", "--subs", "stocks"])
        self.assertFalse(plain.engagement)
        flagged = parser.parse_args(["search", "q", "--subs", "stocks", "--engagement"])
        self.assertTrue(flagged.engagement)
        hot = parser.parse_args(["hot", "--subs", "stocks"])
        self.assertTrue(hot.engagement)
        self.assertEqual(hot.since, "36h")
        self.assertEqual(hot.engagement_top, 10)

    def test_client_errors_do_not_retry_even_with_reset_header(self):
        for code in (400, 404):
            with self.subTest(code=code):
                self.slept.clear()
                net = ScriptedHttp([
                    http_reply(code, '{"error":"no"}', {"X-RateLimit-Reset": "40"}),
                ])
                mod._http_get = net
                with self.assertRaises(mod.FatalBackendError):
                    mod.fetch_json("https://arctic-shift.photon-reddit.com/api/posts/search?x=1")
                self.assertEqual(len(net.urls), 1)
                self.assertEqual(self.slept, [])

    def test_429_waits_for_header_and_not_after_final_attempt(self):
        net = ScriptedHttp([
            http_reply(429, "slow down", {"X-RateLimit-Reset": "30"}),
            http_reply(200, {"data": []}),
        ])
        mod._http_get = net
        payload = mod.fetch_json("https://arctic-shift.photon-reddit.com/api/posts/search?x=1")
        self.assertEqual(payload, {"data": []})
        self.assertEqual(len(net.urls), 2)
        self.assertEqual(len(self.slept), 1)
        self.assertGreaterEqual(self.slept[0], 30)
        self.assertLessEqual(self.slept[0], 30.75)

        self.slept.clear()
        net = ScriptedHttp([
            http_reply(429, "slow down", {"X-RateLimit-Reset": "10"}),
            http_reply(429, "slow down", {"X-RateLimit-Reset": "10"}),
            http_reply(429, "slow down", {"X-RateLimit-Reset": "10"}),
        ])
        mod._http_get = net
        with self.assertRaises(mod.BackendError):
            mod.fetch_json("https://arctic-shift.photon-reddit.com/api/posts/search?x=1")
        self.assertEqual(len(net.urls), 3)
        self.assertEqual(len(self.slept), 2)

        self.slept.clear()
        net = ScriptedHttp([
            http_reply(429, "slow down", {"X-RateLimit-Reset": "100"}),
            http_reply(200, {"data": {"ok": True}}),
        ])
        mod._http_get = net
        mod.fetch_json("https://arctic-shift.photon-reddit.com/api/posts/search?x=1")
        self.assertEqual(self.slept, [mod.RATE_LIMIT_CAP])

    def test_422_and_5xx_use_jitter_not_the_rate_limit_header(self):
        net = ScriptedHttp([
            http_reply(422, '{"error":"Timeout. Maybe slow down a bit"}', {"X-RateLimit-Reset": "40"}),
            http_reply(200, {"data": [1]}),
        ])
        mod._http_get = net
        self.assertEqual(mod.fetch_json("https://arctic-shift.photon-reddit.com/x"), {"data": [1]})
        self.assertEqual(self.slept, [1.0])

        self.slept.clear()
        net = ScriptedHttp([
            http_reply(503, "unavailable", {"X-RateLimit-Reset": "40"}),
            http_reply(503, "unavailable", {"X-RateLimit-Reset": "40"}),
            http_reply(200, {"data": []}),
        ])
        mod._http_get = net
        mod.fetch_json("https://arctic-shift.photon-reddit.com/x")
        # attempt 0 -> 1s, attempt 1 -> 2s, uniform patched to the high end
        self.assertEqual(self.slept, [1.0, 2.0])

    def test_pullpush_refusal_is_not_retried(self):
        net = ScriptedHttp([
            http_reply(
                429,
                "This website does not provide free scraping resources for agents",
            ),
            http_reply(200, {"data": [{"id": "should-not-fetch"}]}),
        ])
        mod._http_get = net
        with self.assertRaises(mod.FatalBackendError) as ctx:
            mod.fetch_json("https://api.pullpush.io/topic?q=x")
        self.assertIn("agents", str(ctx.exception).lower())
        self.assertEqual(len(net.urls), 1)
        self.assertEqual(self.slept, [])

    def test_rate_limit_header_parsing(self):
        self.assertEqual(mod.rate_limit_seconds({"X-RateLimit-Reset": "25"}), 25.0)
        now = mod.time.time()
        headers = {"X-RateLimit-Reset-At": str((now + 12) * 1000)}
        self.assertAlmostEqual(mod.rate_limit_seconds(headers), 12.0, delta=1.0)
        # Reset wins over Reset-At when both are present.
        both = {"X-RateLimit-Reset": "8", "X-RateLimit-Reset-At": str((now + 100) * 1000)}
        self.assertEqual(mod.rate_limit_seconds(both), 8.0)

    def test_window_paging_sends_after_and_exclusive_before(self):
        after = 1_700_000_000
        calls = []

        def fetch(url, **_kwargs):
            calls.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "before" not in query:
                rows = [
                    {"id": f"p{i}", "created_utc": 1_700_008_000 - i, "title": "t", "subreddit": "stocks"}
                    for i in range(mod.PAGE_SIZE)
                ]
                return {"data": rows}
            self.assertEqual(query["before"][0], str(1_700_008_000 - (mod.PAGE_SIZE - 1)))
            self.assertEqual(query["after"][0], str(after))
            self.assertNotIn("permalink", query["fields"][0])
            oldest = 1_700_008_000 - (mod.PAGE_SIZE - 1)
            rows = [
                {"id": f"q{i}", "created_utc": oldest - 1 - i, "title": "t", "subreddit": "stocks"}
                for i in range(10)
            ]
            return {"data": rows}

        rows, truncated = mod.ArcticShiftBackend(fetch=fetch).search_window("earnings", "stocks", after)
        self.assertFalse(truncated)
        self.assertEqual(len(calls), 2)
        self.assertIn("query=earnings", calls[0])
        self.assertEqual(len(rows), mod.PAGE_SIZE + 10)
        self.assertEqual(len({row["id"] for row in rows}), len(rows))

    def test_window_truncated_at_page_cap(self):
        mod.MAX_WINDOW_POSTS = mod.PAGE_SIZE * 2
        after = 100

        def fetch(url, **_kwargs):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            # Stay strictly above `after` so the scan never observes the window start.
            if "before" not in query:
                start = 10_000
            else:
                start = int(float(query["before"][0])) - 1
            return {
                "data": [
                    {"id": f"{start}-{i}", "created_utc": start - i, "title": "t"}
                    for i in range(mod.PAGE_SIZE)
                ]
            }

        rows, truncated = mod.ArcticShiftBackend(fetch=fetch).search_window("", "stocks", after)
        self.assertTrue(truncated)
        self.assertEqual(len(rows), 2 * mod.PAGE_SIZE)

    def test_timeout_shrinks_page_and_keeps_going(self):
        calls = []

        def fetch(url, **_kwargs):
            calls.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            limit = int(query["limit"][0])
            if limit > 50:
                raise mod.BackendError('HTTP 422 {"error":"Timeout. Maybe slow down a bit"}')
            return {"data": [{
                "id": "a",
                "created_utc": 5_000,
                "title": "t",
                "subreddit": "wallstreetbets",
                "selftext": "",
                "score": 1,
                "num_comments": 0,
            }]}

        err = io.StringIO()
        with redirect_stderr(err):
            rows, truncated = mod.ArcticShiftBackend(fetch=fetch).search_window("", "wallstreetbets", 1_000)
        self.assertFalse(truncated)
        self.assertEqual([row["id"] for row in rows], ["a"])
        self.assertEqual(len(calls), 2)
        self.assertIn("limit=100", calls[0])
        self.assertIn("limit=50", calls[1])
        self.assertIn("limit=50", err.getvalue())

    def test_keyword_fallback_matches_every_term_and_runs_once(self):
        now = mod.time.time()
        calls = []

        def search_window(self, query, sub, after, before=None, budget=None):
            calls.append(query)
            if query:
                return [], False
            return [
                raw_post("only", now - 100, title="alpha only"),
                raw_post("both", now - 50, title="beta then alpha"),
            ], False

        mod.ArcticShiftBackend.search_window = search_window
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mod.main(["search", "alpha beta", "--subs", "stocks", "--since", "24h", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["alpha beta", ""])
        payload = json.loads(out.getvalue())
        self.assertEqual([post["id"] for post in payload["posts"]], ["both"])
        self.assertIn("browse+filter once", err.getvalue())

        calls.clear()

        def search_window_raises(self, query, sub, after, before=None, budget=None):
            calls.append(query)
            if query:
                raise mod.BackendError("timeout")
            return [], False

        mod.ArcticShiftBackend.search_window = search_window_raises
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = mod.main(["search", "alpha beta", "--subs", "stocks", "--since", "24h", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["alpha beta", ""])

        calls.clear()

        def search_window_hit(self, query, sub, after, before=None, budget=None):
            calls.append(query)
            return [raw_post("hit", now - 10, title="alpha beta")], False

        mod.ArcticShiftBackend.search_window = search_window_hit
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = mod.main(["search", "alpha beta", "--subs", "stocks", "--since", "24h", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["alpha beta"])

    def test_auto_does_not_call_pullpush(self):
        net = ScriptedHttp([http_reply(200, {"data": []}) for _ in range(6)])
        mod._http_get = net
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = mod.main(["search", "zzz", "--subs", "stocks", "--since", "1h", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertTrue(net.urls)
        self.assertTrue(all("arctic-shift.photon-reddit.com" in url for url in net.urls))
        self.assertTrue(all("pullpush" not in url for url in net.urls))

    def test_removed_posts_permalink_and_excerpt(self):
        now = 1_800_000_000.0
        self.assertIsNone(mod.normalize_post(
            raw_post("x", now - 10, title="[deleted]", selftext="still here"), 200, now,
        ))
        flagged = mod.normalize_post(
            raw_post("y", now - 10, title="Useful title", selftext="[removed]"), 200, now,
        )
        self.assertTrue(flagged["removed"])
        self.assertEqual(flagged["excerpt"], "")
        missing_sub = mod.normalize_post(
            {"id": "abc", "title": "Hi", "created_utc": now - 10, "score": 1, "selftext": "z"},
            200,
            now,
        )
        self.assertEqual(missing_sub["subreddit"], "")
        self.assertEqual(missing_sub["url"], "https://www.reddit.com/comments/abc/")
        self.assertNotIn("/r/None/", missing_sub["url"])

        long_body = "word " * 80
        long_post = mod.normalize_post(raw_post("z", now - 10, selftext=long_body), 250, now)
        self.assertGreater(len(long_post["excerpt"]), 200)
        table = mod.render_table([long_post], engagement=False)
        self.assertIn(long_post["excerpt"], table)
        table.encode("gbk")
        self.assertIn("| pending |", table)
        self.assertNotIn("👍", table)

        old = mod.normalize_post(
            raw_post("old", now - 50 * 3600, title="Old", score=100, num_comments=12),
            80,
            now,
        )
        self.assertEqual(old["score_status"], "backfilled")
        self.assertIn("| 100 |", mod.render_table([old], engagement=False))

    def test_write_out_gbk_replaces(self):
        buf = io.BytesIO()
        wrapper = io.TextIOWrapper(buf, encoding="gbk", errors="strict")
        previous = mod.sys.stdout
        try:
            mod.sys.stdout = wrapper
            mod.write_out("score pending \U0001F44D")
        finally:
            mod.sys.stdout = previous
        decoded = buf.getvalue().decode("gbk")
        self.assertIn("score pending", decoded)

    def test_comment_tree_bfs_skips_removed_and_more(self):
        def fetch(url, pace=None):
            return {"data": [{
                "kind": "t1",
                "data": {
                    "id": "parent",
                    "score": 1,
                    "body": "[removed]",
                    "replies": {"data": {"children": [
                        {"kind": "t1", "data": {"id": "child", "score": 5, "body": "visible", "replies": ""}},
                        {"kind": "more", "data": {"children": ["skipped"]}},
                        {"kind": "t1", "data": {"id": "low", "score": 2, "body": "also", "replies": ""}},
                    ]}},
                },
            }]}

        comments = mod.ArcticShiftBackend(fetch=fetch).get_comments("xyz", 10)
        self.assertEqual([row["id"] for row in comments], ["child", "low"])
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(".pop(0)", source)

    def test_comment_count_pages_and_caps(self):
        def fetch(url, pace=None):
            self.assertIn("/api/comments/search", url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "before" not in query:
                return {"data": [{"id": f"c{i}", "created_utc": 5000 - i} for i in range(100)]}
            return {"data": [{"id": f"d{i}", "created_utc": 4800 - i} for i in range(10)]}

        count, capped = mod.ArcticShiftBackend(fetch=fetch).count_comments("t3_abc")
        self.assertEqual(count, 110)
        self.assertFalse(capped)

        def always_full(url, pace=None):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            start = 9000 if "before" not in query else int(float(query["before"][0])) - 1
            return {"data": [{"id": f"{start}-{i}", "created_utc": start - i} for i in range(100)]}

        count, capped = mod.ArcticShiftBackend(fetch=always_full).count_comments("abc")
        self.assertEqual(count, 300)
        self.assertTrue(capped)

    def test_engagement_rank_cache_and_min_score(self):
        now = 1_800_000_000.0
        young = mod.normalize_post(raw_post("young", now - 2 * 3600, score=1, num_comments=0), 80, now)
        quiet = mod.normalize_post(raw_post("quiet", now - 3600, score=1, num_comments=0), 80, now)
        old = mod.normalize_post(
            raw_post("old", now - 48 * 3600, score=100, num_comments=10), 80, now,
        )
        mod.annotate_live_comments(young, 6, False, now)
        # 2h old, above the 15min floor: 6/2 = 3
        self.assertEqual(young["comments_per_hour"], 3.0)
        fresh = mod.normalize_post(raw_post("fresh", now - 60, score=1), 80, now)
        mod.annotate_live_comments(fresh, 6, False, now)
        self.assertEqual(fresh["comments_per_hour"], round(6 / mod.CPH_MIN_AGE_HOURS, 2))
        ranked = mod.rank_posts([old, quiet, young], now, engagement=True)
        self.assertEqual([post["id"] for post in ranked], ["young", "old", "quiet"])

        posts = []
        for i in range(10):
            posts.append(mod.normalize_post(
                raw_post(f"p{i}", now - (10 - i) * 3600, score=1, num_comments=0), 40, now,
            ))
        chosen = mod.select_engagement_candidates(posts, now, 3)
        self.assertEqual(len(chosen), 3)
        ids = {post["id"] for post in chosen}
        self.assertIn("p0", ids)
        self.assertIn("p9", ids)
        signaled = mod.normalize_post(
            raw_post("sig", now - 5 * 3600, score=1, num_comments=4), 40, now,
        )
        only = mod.select_engagement_candidates(posts + [signaled], now, 1)
        self.assertEqual([post["id"] for post in only], ["sig"])

        path = pathlib.Path(self._tmpdir.name) / "cache.json"
        cache = mod.EngagementCache(path, now)
        cache.put("young", 9, False)
        reloaded = mod.EngagementCache(path, now)
        calls = []

        def counter(pid):
            calls.append(pid)
            return 1, False

        measured, failed = mod.measure_engagement([young], [young], counter, reloaded, now)
        self.assertEqual(calls, [])
        self.assertEqual(measured, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(young["num_comments"], 9)
        expired = mod.EngagementCache(path, now + mod.CACHE_TTL_SECONDS + 5)
        self.assertIsNone(expired.get("young"))

        kept, info = mod.prepare_posts(
            [
                raw_post("pending", now - 3600, score=1),
                raw_post("low", now - 50 * 3600, score=5, num_comments=1),
                raw_post("high", now - 50 * 3600, score=20, num_comments=1),
            ],
            now_ts=now,
            excerpt_chars=80,
            min_score=10,
            limit=10,
            engagement=False,
            engagement_top=8,
        )
        self.assertEqual({post["id"] for post in kept}, {"pending", "high"})
        self.assertEqual(info["min_score_kept_pending"], 1)

    def test_hot_measures_only_the_cap(self):
        now = mod.time.time()
        calls = []

        def search_window(self, query, sub, after, before=None, budget=None):
            return [
                raw_post(f"id{i}", now - (i + 1) * 60, title=f"title {i}")
                for i in range(6)
            ], False

        def count_comments(self, pid):
            calls.append(pid)
            return 2, False

        mod.ArcticShiftBackend.search_window = search_window
        mod.ArcticShiftBackend.count_comments = count_comments
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = mod.main([
                "hot", "--subs", "stocks", "--since", "24h", "--limit", "5",
                "--engagement-top", "3", "--format", "json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        payload = json.loads(out.getvalue())
        live = [post for post in payload["posts"] if post.get("comments_source") == "live"]
        self.assertEqual(len(live), 3)
        self.assertTrue(all(post["score_status"] == "pending" for post in payload["posts"]))
        self.assertIn("comments_per_hour", live[0])

        calls.clear()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = mod.main(["search", "", "--subs", "stocks", "--since", "24h", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])


    def test_split_aliases(self):
        self.assertEqual(mod.split_aliases("a|b|c"), ["a", "b", "c"])
        self.assertEqual(mod.split_aliases("  a || b  "), ["a", "b"])
        self.assertEqual(mod.split_aliases(""), [""])
        self.assertEqual(mod.split_aliases("single"), ["single"])

    def test_word_boundary_matching(self):
        # "1.6T" must not match "$6 trillion" (substring false positive).
        self.assertFalse(mod.matches_terms(
            {"title": "Market cap near $6 trillion", "selftext": ""}, ["1.6t"]))
        self.assertTrue(mod.matches_terms(
            {"title": "New 1.6T optical modules shipping", "selftext": ""}, ["1.6t"]))
        # Standalone acronyms don't match longer words.
        self.assertTrue(mod.matches_terms(
            {"title": "CPO vs pluggable", "selftext": ""}, ["cpo"]))
        self.assertFalse(mod.matches_terms(
            {"title": "SCPO adapters", "selftext": ""}, ["cpo"]))
        # English plurals still match the singular term.
        self.assertTrue(mod.matches_terms(
            {"title": "optical interconnects are hot", "selftext": ""},
            ["optical", "interconnect"]))
        # ...but the false positive stays fixed.
        self.assertFalse(mod.matches_terms(
            {"title": "Market cap near $6 trillions", "selftext": ""}, ["1.6t"]))
        # CJK has no word boundaries: substring fallback.
        self.assertTrue(mod.matches_terms(
            {"title": "这是光互连技术", "selftext": ""}, ["光互连"]))
        # AND within one alias still holds (server parity).
        self.assertFalse(mod.matches_terms(
            {"title": "alpha only", "selftext": ""}, ["alpha", "beta"]))
        self.assertTrue(mod.matches_terms(
            {"title": "beta then alpha", "selftext": ""}, ["alpha", "beta"]))

    def test_alias_merge_and_boost(self):
        now = mod.time.time()

        def search_window(self, query, sub, after, before=None, budget=None):
            if query == "alpha":
                return [raw_post("a1", now - 100, title="alpha here", score=10)], False
            if query == "beta":
                return [
                    raw_post("a1", now - 100, title="alpha here", score=10),
                    raw_post("b1", now - 100, title="beta here", score=10),
                ], False
            return [], False

        mod.ArcticShiftBackend.search_window = search_window
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = mod.main([
                "search", "alpha|beta", "--subs", "stocks", "--since", "24h",
                "--format", "json",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        ids = [post["id"] for post in payload["posts"]]
        # a1 matched both aliases: same gravity as b1, boosted first.
        self.assertEqual(ids, ["a1", "b1"])
        # Internal ranking signal never leaks into output.
        self.assertTrue(all("_alias_hits" not in post for post in payload["posts"]))

    def test_time_slices(self):
        now = 1_800_000_000.0
        day = 86400.0
        self.assertEqual(len(mod._time_slices(now - day, now)), 1)
        self.assertEqual(len(mod._time_slices(now - 7 * day, now)), 7)
        self.assertEqual(len(mod._time_slices(now - 30 * day, now)), 10)
        slices = mod._time_slices(now - 7 * day, now)
        self.assertIsNone(slices[0][1])  # newest slice unbounded on top
        self.assertEqual(slices[-1][0], now - 7 * day)  # oldest reaches the window start
        # Slices are contiguous and ordered newest-first.
        for (lo_a, hi_a), (lo_b, hi_b) in zip(slices, slices[1:]):
            self.assertEqual(lo_a, hi_b)

    def test_removed_posts_dropped_by_default(self):
        now = 1_800_000_000.0
        raws = [
            raw_post("gone", now - 100, title="Some title", selftext="[removed]"),
            raw_post("kept", now - 100, title="Fine title", selftext="body"),
        ]
        base = dict(now_ts=now, excerpt_chars=80, min_score=0, limit=10,
                    engagement=False, engagement_top=8)
        kept, _ = mod.prepare_posts(raws, **base)
        self.assertEqual([post["id"] for post in kept], ["kept"])
        kept, _ = mod.prepare_posts(raws, drop_removed=False, **base)
        self.assertEqual({post["id"] for post in kept}, {"gone", "kept"})

    def test_min_comments_keeps_pending(self):
        now = 1_800_000_000.0
        raws = [
            raw_post("young", now - 3600, score=1, num_comments=0),
            raw_post("old_low", now - 50 * 3600, score=5, num_comments=1),
            raw_post("old_high", now - 50 * 3600, score=20, num_comments=8),
        ]
        kept, _ = mod.prepare_posts(
            raws, now_ts=now, excerpt_chars=80, min_score=0, min_comments=5,
            limit=10, engagement=False, engagement_top=8,
        )
        self.assertEqual({post["id"] for post in kept}, {"young", "old_high"})

    def test_pacer_degrades_and_recovers(self):
        pacer = mod.AdaptivePacer(rate=2.0, capacity=2, min_rate=0.5, max_rate=4.0)
        pacer.on_congestion()
        self.assertAlmostEqual(pacer._rate, 1.0)
        pacer.on_congestion()
        self.assertAlmostEqual(pacer._rate, 0.5)
        pacer.on_congestion()
        self.assertAlmostEqual(pacer._rate, 0.5)  # floor holds
        for _ in range(300):
            pacer.on_success()
        self.assertAlmostEqual(pacer._rate, 4.0)  # ceiling holds

    def test_proxy_for_url(self):
        saved = dict(os.environ)
        try:
            for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
                        "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
                os.environ.pop(key, None)
            self.assertIsNone(mod._proxy_for_url("https", "example.com"))
            os.environ["https_proxy"] = "http://proxy:3128"
            self.assertEqual(
                mod._proxy_for_url("https", "example.com"), "http://proxy:3128")
            os.environ["no_proxy"] = "example.com"
            self.assertIsNone(mod._proxy_for_url("https", "example.com"))
            self.assertEqual(
                mod._proxy_for_url("https", "other.com"), "http://proxy:3128")
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
