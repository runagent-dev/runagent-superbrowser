import asyncio
import json

from eval.core.judges import judge_run_async, primary_success, read_verdicts
from eval.core.judges import webjudge as wj
from eval.core.judges.answer_judge import heuristic_success, looks_like_api_error
from eval.core.judges.base import Verdict, action_history
from eval.core.judges.deterministic import evaluate_checks
from eval.core.tasks import Task
from eval.tests.helpers import FakeChatClient, make_run_dir


def test_webjudge_parsers():
    assert wj.parse_score("**Reasoning**: filters visible\n\n**Score**: 4") == (4, "filters visible")
    assert wj.parse_score("Reasoning: blah\nScore: 2")[0] == 2
    assert wj.parse_score("garbage")[0] == 1
    assert wj.parse_status('Thoughts: all key points met\nStatus: "success"') is True
    assert wj.parse_status("Status: failure") is False
    assert wj.parse_key_points("intro **Key Points**:\n1. red\n2. 2018-2023") == "1. red\n2. 2018-2023"


def test_action_history_and_heuristics():
    tr = [{"messages": [
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "browser_click_at",
                                                                      "arguments": json.dumps({"vision_index": 3})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]}]
    assert action_history(tr) == ['browser_click_at({"vision_index": 3})']
    assert looks_like_api_error("Error: exceeded your current quota") is True
    assert heuristic_success("Browser worker failed: captcha_unsolved") is False
    assert heuristic_success("Found 2 rabbits") is True


def test_deterministic_checks():
    res = evaluate_checks([
        {"type": "final_url_regex", "pattern": r"breed=English%20Spot", "flags": "i"},
        {"type": "answer_contains_all", "values": ["Chicago", "English Spot"]},
        {"type": "visited_url_regex", "pattern": r"age=Young"},
        {"type": "answer_regex", "pattern": r"\$\d+"},
    ], final_answer="Two English Spot rabbits in Chicago, $0 fee", last_url="https://p.com/s?breed=english%20spot",
       visited=["https://p.com/", "https://p.com/s?age=Young"])
    assert [r["ok"] for r in res] == [True, True, True, True]


def test_webjudge_end_to_end_with_fake_client(tmp_path):
    run_dir = make_run_dir(tmp_path, screenshots=3)
    task = Task.from_row(json.loads((run_dir / "spec.json").read_text())["task"])

    def responder(kwargs):
        sys_prompt = kwargs["messages"][0]["content"]
        if sys_prompt.startswith("You are an expert tasked with analyzing"):
            return "**Key Points**:\n1. red Toyota Corolla\n2. model years 2018 to 2023"
        if sys_prompt.startswith("You are an expert evaluator tasked"):
            user = kwargs["messages"][1]["content"]
            assert user[1]["type"] == "image_url" and user[1]["image_url"]["url"].startswith("data:image/png;base64,")
            return "**Reasoning**: shows the year filter\n**Score**: 4"
        assert "Action History" in kwargs["messages"][1]["content"][0]["text"]
        n_images = sum(1 for part in kwargs["messages"][1]["content"] if part["type"] == "image_url")
        assert n_images == 3
        return "Thoughts: every key point is satisfied\nStatus: \"success\""

    client = FakeChatClient(responder=responder)
    verdicts = asyncio.run(judge_run_async(run_dir, task, which=["webjudge", "deterministic"], client=client))
    v = verdicts["webjudge"]
    assert v.success is True and v.judge == "webjudge"
    assert v.details["screenshots_evaluated"] == 3 and v.details["screenshots_relevant"] == 3
    assert v.usage["calls"] == 5  # 1 key-point + 3 scoring + 1 outcome
    assert verdicts["deterministic"].success is None
    stored = read_verdicts(run_dir)
    assert stored["webjudge"].success is True
    assert primary_success(stored) == (True, "webjudge")
    # second call is a no-op (verdict cached on disk)
    again = asyncio.run(judge_run_async(run_dir, task, which=["webjudge"], client=FakeChatClient()))
    assert again["webjudge"].success is True


def test_answer_judge_with_fake_client(tmp_path):
    run_dir = make_run_dir(tmp_path, task_id="t9")
    task = Task.from_row(json.loads((run_dir / "spec.json").read_text())["task"])
    client = FakeChatClient(['{"success": false, "rationale": "no listing data"}'])
    verdicts = asyncio.run(judge_run_async(run_dir, task, which=["answer_judge"], client=client))
    assert verdicts["answer_judge"].success is False
    assert primary_success(verdicts) == (False, "answer_judge")
    assert Verdict.from_dict(verdicts["answer_judge"].to_dict()).rationale == "no listing data"


def test_webjudge_falls_back_when_model_rejects_max_tokens(tmp_path):
    run_dir = make_run_dir(tmp_path, task_id="t11", screenshots=1)
    task = Task.from_row(json.loads((run_dir / "spec.json").read_text())["task"])
    calls = []

    class Rejecting(FakeChatClient):
        async def create(self, **kwargs):
            calls.append(sorted(k for k in kwargs if k in ("temperature", "max_tokens", "max_completion_tokens")))
            if "max_tokens" in kwargs:
                raise RuntimeError("Unsupported parameter: 'max_tokens' is not supported with this model.")
            return await super().create(**kwargs)

    def responder(kwargs):
        sys_prompt = kwargs["messages"][0]["content"]
        if sys_prompt.startswith("You are an expert tasked with analyzing"):
            return "**Key Points**:\n1. x"
        if sys_prompt.startswith("You are an expert evaluator tasked"):
            return "**Score**: 2"
        return "Status: failure"

    client = Rejecting(responder=responder)
    verdicts = asyncio.run(judge_run_async(run_dir, task, which=["webjudge"], client=client))
    assert verdicts["webjudge"].success is False
    assert calls[0] == ["max_tokens", "temperature"] and calls[1] == ["max_completion_tokens"]


def test_resolve_client_scopes_credentials_per_judge(monkeypatch, tmp_path):
    """A judge with its own key must not inherit another judge's base URL.

    Regression: the shared SUPERBROWSER_EVAL_JUDGE_* pair pointed at a Gemini
    endpoint, so WebJudge (default model gpt-4o) was silently routed there and
    would have failed on every run of a sweep.
    """
    from eval.core.judges.base import resolve_client

    for k in ("SUPERBROWSER_EVAL_WEBJUDGE_API_KEY", "SUPERBROWSER_EVAL_WEBJUDGE_BASE_URL",
              "SUPERBROWSER_EVAL_JUDGE_API_KEY", "SUPERBROWSER_EVAL_JUDGE_BASE_URL",
              "SUPERBROWSER_EVAL_ANSWER_JUDGE_API_KEY", "SUPERBROWSER_EVAL_ANSWER_JUDGE_BASE_URL",
              "OPENAI_API_KEY", "SUPERBROWSER_EVAL_WEBJUDGE_MODEL", "SUPERBROWSER_EVAL_JUDGE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    # config fallback must not leak into this test
    monkeypatch.setattr("eval.core.judges.base.DEFAULT_CONFIG_PATH", str(tmp_path / "none.json"))

    # shared pair only -> both judges use it (back-compatible)
    monkeypatch.setenv("SUPERBROWSER_EVAL_JUDGE_API_KEY", "shared-key")
    monkeypatch.setenv("SUPERBROWSER_EVAL_JUDGE_BASE_URL", "https://gemini.example/v1")
    web, _ = resolve_client(model_env="SUPERBROWSER_EVAL_WEBJUDGE_MODEL", default_model="gpt-4o",
                            prefix="SUPERBROWSER_EVAL_WEBJUDGE")
    assert "gemini.example" in str(web.base_url)

    # scoped key for WebJudge -> its own endpoint (default OpenAI), shared pair untouched
    monkeypatch.setenv("SUPERBROWSER_EVAL_WEBJUDGE_API_KEY", "web-key")
    web, model = resolve_client(model_env="SUPERBROWSER_EVAL_WEBJUDGE_MODEL", default_model="gpt-4o",
                                prefix="SUPERBROWSER_EVAL_WEBJUDGE")
    assert model == "gpt-4o" and web.api_key == "web-key"
    assert "gemini.example" not in str(web.base_url)
    ans, _ = resolve_client(model_env="SUPERBROWSER_EVAL_JUDGE_MODEL", default_model="gpt-5.5",
                            prefix="SUPERBROWSER_EVAL_ANSWER_JUDGE")
    assert ans.api_key == "shared-key" and "gemini.example" in str(ans.base_url)

    # a scoped base URL travels with the scoped key
    monkeypatch.setenv("SUPERBROWSER_EVAL_WEBJUDGE_BASE_URL", "https://oai.example/v1")
    web, _ = resolve_client(model_env="SUPERBROWSER_EVAL_WEBJUDGE_MODEL", default_model="gpt-4o",
                            prefix="SUPERBROWSER_EVAL_WEBJUDGE")
    assert "oai.example" in str(web.base_url)


def test_preflight_flags_model_endpoint_mismatch(monkeypatch, tmp_path):
    """The preflight must fail loudly on the exact misconfiguration above."""
    from eval import preflight

    for k in ("SUPERBROWSER_EVAL_WEBJUDGE_API_KEY", "SUPERBROWSER_EVAL_WEBJUDGE_BASE_URL",
              "SUPERBROWSER_EVAL_ANSWER_JUDGE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("eval.core.judges.base.DEFAULT_CONFIG_PATH", str(tmp_path / "none.json"))
    monkeypatch.setenv("SUPERBROWSER_EVAL_JUDGE_API_KEY", "k")
    monkeypatch.setenv("SUPERBROWSER_EVAL_JUDGE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
    monkeypatch.setenv("SUPERBROWSER_EVAL_WEBJUDGE_MODEL", "gpt-4o")
    monkeypatch.setenv("SUPERBROWSER_EVAL_JUDGE_MODEL", "gemini-3-flash-preview")

    rep = preflight.Report()
    preflight.check_judges(rep)
    assert rep.failed
    assert any("cannot be served by google endpoint" in r[2] for r in rep.rows if r[0] == preflight.BAD)

    # matched pair -> no failure
    monkeypatch.setenv("SUPERBROWSER_EVAL_WEBJUDGE_MODEL", "gemini-3-flash-preview")
    rep2 = preflight.Report()
    preflight.check_judges(rep2)
    assert not rep2.failed


def test_webjudge_reuses_the_working_parameter_shape(tmp_path):
    """A reasoning model rejects max_tokens on every call; the first rejection
    must be learned, not repaid once per screenshot."""
    import asyncio as _a
    from eval.core.judges.webjudge import WebJudge

    class Counting:
        def __init__(self):
            self.shapes = []
            self.chat = self
            self.completions = self

        async def create(self, *, model, messages, **kwargs):
            self.shapes.append(tuple(sorted(kwargs)))
            if "max_tokens" in kwargs:
                raise RuntimeError("Unsupported parameter: 'max_tokens' is not supported with this model")
            return FakeChatClient.response("**Reasoning**: ok\n**Score**: 4")

    c = Counting()
    j = WebJudge(c, "gpt-5.4-mini")
    for _ in range(3):
        _a.run(j._chat([{"role": "user", "content": "x"}], max_tokens=512))
    # first call probes max_tokens then succeeds; later calls go straight to the working shape
    assert c.shapes[0] == ("max_tokens", "temperature")
    assert c.shapes[1:] == [("max_completion_tokens",)] * 3


def test_webjudge_retries_when_a_token_cap_swallows_the_answer(tmp_path):
    """Empty content + finish_reason 'length' must not reach the parsers as a 'no'."""
    import asyncio as _a
    from eval.core.judges.webjudge import WebJudge

    class Capped:
        def __init__(self):
            self.caps = []
            self.chat = self
            self.completions = self

        async def create(self, *, model, messages, **kwargs):
            cap = kwargs.get("max_completion_tokens") or kwargs.get("max_tokens")
            self.caps.append(cap)
            if cap and cap <= 512:      # all budget burned on hidden reasoning
                return FakeChatClient.response("", finish_reason="length")
            return FakeChatClient.response("**Reasoning**: fine\n**Score**: 4")

    c = Capped()
    j = WebJudge(c, "gpt-5.4-mini")
    j._shape = "max_completion_tokens"
    out = _a.run(j._chat([{"role": "user", "content": "x"}], max_tokens=512))
    assert "Score" in out and c.caps == [512, 2048]
