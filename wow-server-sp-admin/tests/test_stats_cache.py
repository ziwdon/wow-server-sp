import json
import time
from unittest.mock import patch

from app.services import stats_cache
from app.services.players import PvpRankRow, RankRow
from app.services.stats import Bucket, StackedBucket, StackedSegment, StatsSnapshot


class _PausedThread:
    """Thread stand-in that lets tests observe refresh state before it runs."""

    started: list["_PausedThread"] = []

    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.started.append(self)

    def run(self):
        self.target(*self.args)


def _snap(fetched_at: float) -> StatsSnapshot:
    return StatsSnapshot(
        fetched_at=fetched_at,
        bots_total=2500, bots_online=200, players_total=3, players_online=1,
        ahbot_total=4, ahbot_online=0,
        bots_by_class=[Bucket("Warrior", 400)],
        bots_by_bracket_stacked=[
            StackedBucket("1-10", 450, (StackedSegment("#88c870", 100), StackedSegment("#888070", 300), StackedSegment("#7ab0e0", 50))),
        ],
        top_pve=(RankRow(1, "Sariel", "Druid", "#FF7C0A", "Night Elf", "Alliance", "#4080C0", 80, 251),),
        top_pvp=(PvpRankRow(1, "Rndslayer", "Hunter", "#AAD372", "Orc", "Horde", "#C03030", 99, 1200),),
    )


def test_json_round_trip(tmp_path):
    r = stats_cache.StatsRefresher(cache_path=tmp_path / "stats" / "snap.json")
    r._store(_snap(123.0))
    r2 = stats_cache.StatsRefresher(cache_path=tmp_path / "stats" / "snap.json")
    r2.load_from_disk()
    got = r2.get()
    assert got is not None
    assert got.bots_total == 2500
    assert got.bots_by_class[0] == Bucket("Warrior", 400)
    assert got.bots_by_bracket_stacked[0].total == 450
    assert got.bots_by_bracket_stacked[0].segments[1].count == 300
    assert got.top_pve[0].name == "Sariel"
    assert got.top_pve[0].avg_ilvl == 251
    assert got.top_pvp[0].name == "Rndslayer"
    assert got.top_pvp[0].honor_kills == 99
    assert got.fetched_at == 123.0


def test_atomic_write_leaves_no_tmp(tmp_path):
    path = tmp_path / "stats" / "snap.json"
    r = stats_cache.StatsRefresher(cache_path=path)
    r._store(_snap(1.0))
    assert path.exists()
    assert not (path.parent / (path.name + ".tmp")).exists()


def test_is_stale_boundaries(tmp_path):
    r = stats_cache.StatsRefresher(cache_path=tmp_path / "s.json")
    assert r.is_stale() is True
    r._store(_snap(time.time()))
    assert r.is_stale() is False
    r._store(_snap(time.time() - 90000))
    assert r.is_stale() is True


def test_load_corrupt_file_is_safe(tmp_path):
    path = tmp_path / "stats" / "snap.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not valid json ::::")
    r = stats_cache.StatsRefresher(cache_path=path)
    r.load_from_disk()
    assert r.get() is None


def test_load_semantically_invalid_json_is_safe(tmp_path):
    path = tmp_path / "stats" / "snap.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "fetched_at": "bad",
        "bots_total": 2500,
        "bots_online": 200,
        "players_total": 3,
        "players_online": 1,
        "ahbot_total": 4,
        "ahbot_online": 0,
        "bots_by_class": [{"label": "Warrior", "count": 400}],
    }))
    r = stats_cache.StatsRefresher(cache_path=path)
    r.load_from_disk()
    assert r.get() is None


def test_load_missing_file_is_safe(tmp_path):
    r = stats_cache.StatsRefresher(cache_path=tmp_path / "nope" / "s.json")
    r.load_from_disk()
    assert r.get() is None


def test_refresh_async_single_flight(tmp_path):
    r = stats_cache.StatsRefresher(cache_path=tmp_path / "s.json")
    started = {"n": 0}

    def fake_collect(**kw):
        started["n"] += 1
        time.sleep(0.2)
        return _snap(time.time())

    with patch("app.services.stats_cache.collect_stats", side_effect=fake_collect):
        first = r.refresh_async({"host": "h", "port": 3306, "user": "u", "password": "p"})
        second = r.refresh_async({"host": "h", "port": 3306, "user": "u", "password": "p"})
        assert first is True
        assert second is False
        for _ in range(50):
            if r.status == "idle":
                break
            time.sleep(0.05)
    assert r.status == "idle"
    assert started["n"] == 1
    assert r.get() is not None


def test_retry_clears_previous_error_while_refreshing_and_on_success(tmp_path):
    r = stats_cache.StatsRefresher(cache_path=tmp_path / "s.json")
    previous = _snap(1.0)
    refreshed = _snap(2.0)
    r._store(previous)

    with patch("app.services.stats_cache.collect_stats", side_effect=RuntimeError("first failure")):
        r._run({})
    assert r.status == "idle"
    assert r.error == "first failure"
    assert r.get() == previous

    _PausedThread.started = []
    with patch("app.services.stats_cache.threading.Thread", _PausedThread), \
         patch("app.services.stats_cache.collect_stats", return_value=refreshed):
        assert r.refresh_async({}) is True
        assert r.status == "refreshing"
        assert r.error is None
        assert r.get() == previous
        _PausedThread.started.pop().run()

    assert r.status == "idle"
    assert r.error is None
    assert r.get() == refreshed


def test_retry_clears_previous_error_while_refreshing_then_reports_latest_failure(tmp_path):
    r = stats_cache.StatsRefresher(cache_path=tmp_path / "s.json")
    previous = _snap(1.0)
    r._store(previous)

    with patch("app.services.stats_cache.collect_stats", side_effect=RuntimeError("first failure")):
        r._run({})
    assert r.status == "idle"
    assert r.error == "first failure"
    assert r.get() == previous

    _PausedThread.started = []
    with patch("app.services.stats_cache.threading.Thread", _PausedThread), \
         patch("app.services.stats_cache.collect_stats", side_effect=RuntimeError("latest failure")):
        assert r.refresh_async({}) is True
        assert r.status == "refreshing"
        assert r.error is None
        assert r.get() == previous
        _PausedThread.started.pop().run()

    assert r.status == "idle"
    assert r.error == "latest failure"
    assert r.get() == previous


def test_disk_write_failure_is_non_fatal(tmp_path):
    r = stats_cache.StatsRefresher(cache_path=tmp_path / "s.json")
    with patch("app.services.stats_cache.os.replace", side_effect=OSError("nope")):
        r._store(_snap(5.0))
    assert r.get() is not None
