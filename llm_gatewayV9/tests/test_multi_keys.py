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
    # Kilo is the vehicle now. These tests exercise the FAN-OUT MECHANISM
    # (singular + plural keys -> -2, -3 siblings), and groq - which they used
    # to use - is no longer constructed at all, so keeping it here would assert
    # a behaviour the gateway no longer has.
    for v in ["GEMINI_API_KEY", "GEMINI_API_KEYS", "NVIDIA_API_KEY",
              "NVIDIA_API_KEYS", "CEREBRAS_API_KEY", "GROQ_API_KEY",
              "GROQ_API_KEYS", "OPEN_ROUTER_API_KEY",
              "GITHUB_ACCESS_TOKEN", "OLLAMA_MODEL"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("KILO_API_KEY", "a")
    monkeypatch.setenv("KILO_API_KEYS", "b,c")
    from cache import GeminiCache
    ws = P.build_providers(GeminiCache())
    assert sorted(ws.keys()) == ["kilo", "kilo-2", "kilo-3"]
    assert ws["kilo-2"].model == ws["kilo"].model
    assert ws["kilo-3"].model == ws["kilo"].model


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
    # Must clear every other key and pin ROUTER_ORDER, because ROUTER_ORDER is
    # an ALLOWLIST that decides which router providers get BUILT.
    for v in ["CEREBRAS_API_KEY", "CEREBRAS_API_KEYS", "GROQ_API_KEY",
              "GROQ_API_KEYS", "NVIDIA_API_KEY", "NVIDIA_API_KEYS",
              "GITHUB_ACCESS_TOKEN", "GITHUB_ACCESS_TOKENS"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("KILO_API_KEYS", raising=False)
    monkeypatch.setenv("KILO_API_KEY", "a")
    monkeypatch.setenv("KILO_API_KEYS", "b")
    monkeypatch.setenv("ROUTER_ORDER", "kilo")
    rp = P.build_router_providers()
    assert sorted(rp.keys()) == ["kilo", "kilo-2"]


def test_disabled_providers_are_never_constructed(monkeypatch):
    """The removal has to hold against a hostile environment.

    Keys present, and every disabled provider named in BOTH allowlists: if any
    of them can still be built, it is still reachable via an explicit
    `provider=` request - which is exactly the "I don't want these at all"
    requirement failing quietly.
    """
    for v in ("CEREBRAS_API_KEY", "CEREBRAS_API_KEYS", "GROQ_API_KEY",
              "GROQ_API_KEYS", "NVIDIA_API_KEY", "NVIDIA_API_KEYS",
              "OPEN_ROUTER_API_KEY", "OPEN_ROUTER_API_KEYS",
              "GITHUB_ACCESS_TOKEN", "GITHUB_ACCESS_TOKENS", "KILO_API_KEY"):
        monkeypatch.setenv(v, "k")
    monkeypatch.setenv("ROUTER_ORDER",
                       "cerebras,groq,nvidia,github,openrouter,kilo")
    from cache import GeminiCache
    workers = P.build_providers(GeminiCache())
    routers = P.build_router_providers()
    for dead in P.DISABLED_PROVIDERS:
        assert dead not in workers, f"{dead} is still a worker"
        assert dead not in routers, f"{dead} is still a router"
        # and no sibling fan-out of it either
        assert not [k for k in workers if k == dead or k.startswith(dead + "-")]
    assert "kilo" in workers and "kilo" in routers


def test_router_order_is_an_allowlist(monkeypatch):
    """A provider left out of ROUTER_ORDER must not even be constructed."""
    for v in ["CEREBRAS_API_KEY", "CEREBRAS_API_KEYS", "GROQ_API_KEY",
              "GROQ_API_KEYS", "NVIDIA_API_KEY", "NVIDIA_API_KEYS",
              "GITHUB_ACCESS_TOKEN", "GITHUB_ACCESS_TOKENS"]:
        monkeypatch.delenv(v, raising=False)
    for v in ("CEREBRAS_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY",
              "GITHUB_ACCESS_TOKEN", "KILO_API_KEY"):
        monkeypatch.setenv(v, "k")
    monkeypatch.setenv("ROUTER_ORDER", "kilo")
    rp = P.build_router_providers()
    assert sorted(rp.keys()) == ["kilo"]
    # Blank ROUTER_ORDER must fall back to the shipped default rather than
    # silently building an empty pool - and the shipped default is now Kilo
    # alone, since the per-model-metered routers were all removed.
    monkeypatch.setenv("ROUTER_ORDER", "")
    rp = P.build_router_providers()
    assert sorted(rp.keys()) == ["kilo"]


def test_kilo_is_a_router_pool_member(monkeypatch):
    """The user has to be able to SEE Kilo in the worker pool to use it."""
    for v in ["CEREBRAS_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY",
              "GITHUB_ACCESS_TOKEN"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("KILO_API_KEYS", raising=False)
    monkeypatch.setenv("KILO_API_KEY", "a")
    monkeypatch.setenv("KILO_API_KEYS", "b")
    rp = P.build_router_providers()
    assert sorted(rp.keys()) == ["kilo", "kilo-2"]
    # Never a retired id, and never a bare name.
    assert rp["kilo"].model and "/" in rp["kilo"].model
    assert rp["kilo"].model != "tencent/hy3:free"


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
