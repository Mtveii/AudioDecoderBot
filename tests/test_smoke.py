import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.setdefault("USAGE_DB_PATH", os.path.join(_tmp, "usage.db"))
os.environ.setdefault("USER_PREFS_PATH", os.path.join(_tmp, "user_prefs.json"))
os.environ.setdefault("BOT_TOKEN", "1:abc")
os.environ.setdefault("STT_MODE", "groq")

import bot


def test_import():
    assert bot.dp is not None
    assert bot.bot is not None


def test_lru_eviction():
    d = bot.LRUDict(2)
    d[1] = "a"
    d[2] = "b"
    assert d.get(1) == "a"  # touch 1
    d[3] = "c"
    assert 2 not in d
    assert 1 in d and 3 in d


def test_blocked_helpers():
    try:
        import stats
    except ImportError:
        print("stats приватный (не в git) — пропуск")
        return
    stats.init_db()
    assert not stats.is_blocked(123)
    stats.block_user(123)
    assert stats.is_blocked(123)
    stats.unblock_user(123)
    assert not stats.is_blocked(123)

if __name__ == "__main__":
    test_import()
    test_lru_eviction()
    test_blocked_helpers()
    print("SMOKE TESTS PASS")
