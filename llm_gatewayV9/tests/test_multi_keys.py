"""Multi-key pools: SINGULAR + comma-separated PLURAL fan out to siblings.

Offline: builds provider objects only (no network), checks naming, limits
resolution and order expansion. Values used here are fake.
"""
import providers as P
from router import limits_for, expand_order, Router


def test_key_list_parsing(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GEMINI_API_KEYS", "k2, k1 ,k3  k2")
    assert P._key_list("GEMINI_API_KEY", "GEMINI_API_KEYS") == ["k1", "k2", "k3"]
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    assert P._key_list("GEMINI_API_KEY", "GEMINI_API_KEYS") == []
    monkeypatch.setenv("GEMINI_API_KEYS", "solo")
    assert P._key_list("GEMINI_API_KEY", "GEMINI_API_KEYS") == ["solo"]


def test_sibling_build(monkeypatch):
    for v in ["GEMINI_API_KEY", "GEMINI_API_KEYS", "NVIDIA_API_KEY",
              "NVIDIA_API_KEYS", "CEREBRAS_API_KEY", "OPEN_ROUTER_API_KEY",
              "GITHUB_ACCESS_TOKEN", "KILO_API_KEY", "OLLAMA_MODEL"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "a")
    monkeypatch.setenv("GROQ_API_KEYS", "b,c")
    from cache import GeminiCache
    ws = P.build_providers(GeminiCache())
    assert sorted(ws.keys()) == ["groq", "groq-2", "groq-3"]
    assert ws["groq-2"].model == ws["groq"].model
    # capabilities resolve through the canonical provider
    assert ws["groq-2"].capabilities.get("tools") is True


def test_gemini_pair_fanout(monkeypatch):
    for v in ["GROQ_API_KEY", "NVIDIA_API_KEY", "CEREBRAS_API_KEY",
              "OPEN_ROUTER_API_KEY", "GITHUB_ACCESS_TOKEN", "KILO_API_KEY",
              "OLLAMA_MODEL"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "a")
    monkeypatch.setenv("GEMINI_API_KEYS", "b")
    from cache import GeminiCache
    ws = P.build_providers(GeminiCache())
    assert sorted(ws.keys()) == ["gemini", "gemini-2", "gemini35lite", "gemini35lite-2"]


def test_router_sibling_build(monkeypatch):
    for v in ["CEREBRAS_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY",
              "GITHUB_ACCESS_TOKEN"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "a")
    monkeypatch.setenv("GROQ_API_KEYS", "b")
    rp = P.build_router_providers()
    assert sorted(rp.keys()) == ["groq", "groq-2"]


def test_keep_gemini_only():
    pool = {"gemini": 1, "gemini-2": 2, "gemini35lite": 3,
            "gemini35lite-2": 4, "groq": 5, "nvidia": 6}
    assert sorted(P.keep_gemini_only(pool).keys()) == [
        "gemini", "gemini-2", "gemini35lite", "gemini35lite-2"]
    assert P.keep_gemini_only({}) == {}
    assert P._base_provider("gemini-2") == "gemini"
    assert P._base_provider("gemini35lite-10") == "gemini35lite"
    assert P._base_provider("groq") == "groq"


def test_write_env_keys_removal(tmp_path, monkeypatch):
    import channels_api as C
    monkeypatch.setattr(C, "_ENV_PATH", tmp_path / ".env")
    C._write_env_keys({"GEMINI_API_KEY": "a", "GEMINI_API_KEYS": "b,c"})
    t = (tmp_path / ".env").read_text()
    assert "GEMINI_API_KEYS=b,c" in t
    C._write_env_keys({"GEMINI_API_KEYS": None})
    t = (tmp_path / ".env").read_text()
    assert "GEMINI_API_KEYS" not in t
    assert "GEMINI_API_KEY=a" in t


def test_pool_members(monkeypatch):
    import channels_api as C
    for v in ["GEMINI_API_KEY", "GEMINI_API_KEYS", "GROQ_API_KEY", "GROQ_API_KEYS"]:
        monkeypatch.delenv(v, raising=False)
    assert C._pool_members("groq") == []
    monkeypatch.setenv("GROQ_API_KEY", "a")
    assert C._pool_members("groq") == ["groq"]
    monkeypatch.setenv("GROQ_API_KEYS", "b")
    assert C._pool_members("groq") == ["groq", "groq-2"]
    monkeypatch.setenv("GEMINI_API_KEY", "a")
    monkeypatch.setenv("GEMINI_API_KEYS", "b")
    assert C._pool_members("gemini") == ["gemini", "gemini35lite", "gemini-2", "gemini35lite-2"]
    assert C._pool_key_count("gemini") == 2
    assert C._pool_key_count("groq") == 2
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    assert C._pool_key_count("groq") == 1


def test_limits_and_expand():
    assert limits_for("gemini-2") == limits_for("gemini")
    assert limits_for("gemini35lite-3") == limits_for("gemini35lite")
    try:
        limits_for("nope-2")
        assert False, "expected KeyError"
    except KeyError:
        pass
    provs = {"gemini": 1, "gemini-2": 1, "groq": 1}
    assert expand_order(["gemini", "groq"], provs) == ["gemini", "gemini-2", "groq"]
    assert expand_order(["groq", "gemini"], provs) == ["groq", "gemini", "gemini-2"]
    r = Router(provs, expand_order(["groq", "gemini"], provs))
    assert r.order == ["groq", "gemini", "gemini-2"]
    assert r.candidates("gemini-2") == ["gemini-2"]
    assert r.candidates("g") == ["gemini"]
    assert r.candidates("nope") == []
