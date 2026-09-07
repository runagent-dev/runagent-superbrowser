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
