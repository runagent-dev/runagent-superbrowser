# Sweep audit — `ablate10`

| Check | Result |
|---|---|
| one_record_per_task_arm | PASS |
| single_protocol_hash | PASS |
| single_webjudge_model | PASS |
| mixed_model_tasks_all_excluded | PASS |
| no_non_browser_tool_executed | PASS |
| all_instruction_mismatches_declared | PASS |
| code_drift_balanced_across_arms_excluding_multi_sha_tasks | PASS |
| no_run_over_snip_threshold | PASS |
| all_records_priced | PASS |

**Shape.** 192 records, 24 tasks, arms fifo, flat, fresh_vision, full_history, ledger, no_deadend, no_ladder, summary; records per task {8: 24}.
**Protocol.** hash {'aef972e8b2217cf5': 192}, judge {'gpt-5.4-mini': 192}, decided_by {'webjudge': 191, 'answer_judge': 1}.
**Host model.** {'z-ai/glm-5.3-flash': 188, 'minimax/minimax-m3': 4}; mixed-model tasks: {'dd44c665cec1e9c929a4c5f074e7844a': {'fifo': 'z-ai/glm-5.3-flash', 'flat': 'minimax/minimax-m3', 'fresh_vision': 'minimax/minimax-m3', 'full_history': 'z-ai/glm-5.3-flash', 'ledger': 'z-ai/glm-5.3-flash', 'no_deadend': 'minimax/minimax-m3', 'no_ladder': 'minimax/minimax-m3', 'summary': 'z-ai/glm-5.3-flash'}}; excluded: {'dd44c665cec1e9c929a4c5f074e7844a': 'host_model_mismatch'}.
**Code drift.** 8 SHAs ['00df212', '33c67a3', '3508dbf', '475dca5', '8fb8ce9', 'a3bbab1', 'a87c2d0', 'b6f7c37'], dirty records 176, SHA histogram identical across arms: False (excluding tasks whose arms span several SHAs: True); tasks spanning several SHAs: {'662ae0f2d3ac851dbcdd245f908277e3': ['33c67a3', '475dca5', 'a3bbab1']}.
**Toolset.** non-browser tools executed: 0; runs attempting one: 54 ({'list_exec_sessions': 47, 'find_files': 20, 'write_stdin': 26, 'exec': 3, 'long_task': 2}).
**Instructions.** 24 checked; mismatches ['85b284c18d7e78c9b5a9e074e7aa3b98']; undeclared [].
**Traces.** vision 0, click 0, context dump 192; grounding source {'tags': 192}; RPR available 0, recovery 0, first-path 143, CSD(obs) 142.
**Context.** max peak prompt overall 90882; snip threshold [182592]; runs over it 0.

| Arm | mean peak | max peak | max ctx est | judge screenshots | infra any | infra dominant |
|---|---|---|---|---|---|---|
| fifo | 43418 | 71084 | 71908 | 14.2 | 1 | 1 |
| flat | 36574 | 49952 | 51239 | 10.5 | 4 | 0 |
| fresh_vision | 43306 | 64318 | 65252 | 4.4 | 1 | 1 |
| full_history | 56229 | 90882 | 92303 | 13.0 | 1 | 1 |
| ledger | 43350 | 57838 | 59500 | 13.5 | 1 | 1 |
| no_deadend | 45348 | 62848 | 110293 | 11.5 | 0 | 0 |
| no_ladder | 46059 | 67690 | 69799 | 15.5 | 1 | 1 |
| summary | 43041 | 65304 | 66444 | 14.5 | 0 | 0 |

**Infra markers.** `ERR_NO_SUPPORTED_PROXIES|ERR_TUNNEL_CONNECTION_FAILED|ERR_PROXY_CONNECTION_FAILED|proxy error|session backend lost|session (?:is )?(?:dead|expired)` (dominant = ≥3 hits).
**Archives under the experiment.** {'_discarded_petfinder': {'entries': 5, 'size_mb': 33.6}, '_discarded_student_com': {'entries': 6, 'size_mb': 18.9}, '_failed_attempts': {'entries': 19, 'size_mb': 15.8}, '_logs': {'entries': 78, 'size_mb': 0.1}, '_orphaned_by_task_swaps': {'entries': 15, 'size_mb': 61.0}, '_preproxy_discarded': {'entries': 1, 'size_mb': 3.7}}.
**Cost / time.** agent $59.47, judge $8.16, total $67.63; summed run time 21.99 h; elapsed 73.2 h; unpriced 0.
**Outcome.** failure reasons {'premature_done': 63, 'bot_block': 15, 'site_unavailable': 3, 'grounding': 1, 'captcha_unsolved': 1, 'timeout': 1}; exclusion labels {'site_unavailable': 3, 'captcha_unsolved': 1}; levels per arm {'hard': 5, 'easy': 16, 'medium': 3}; primary tasks 23.

**Subset.** `ablate24` — 22 task ids supplied by the author, minus 2 whose sites return 403 through our residential proxy, plus the 1 task already completed under this experiment, plus 3 medium-level low-anti-bot fills chosen to restore level balance; every site was loaded in real Chrome through the proxy before freezing

Frozen 2026-09-12 before any of the added arms ran. Excluded for unreachability: b7a9a6b5 (uniqlo.com 403 via residential proxy); 180ed2ec (umich.edu 403 via residential proxy). Fills: 157f4a79, 4e801ba1, 85b284c1. Site reachability was screened, which biases the absolute success rate upward and must be stated when reporting; paired arm comparisons are unaffected. Swapped 0b2623e9 (petfinder.com, hard) out for a11ecdff (coursera.org, easy) at the author's request; coursera verified reachable through the proxy. No completed runs existed for the outgoing task. Swapped 157f4a79 (disney.com) out for 662ae0f2 (wanderlog.com) at the author's request; wanderlog verified reachable through the proxy. One completed run existed for the outgoing task and is orphaned, not counted. Swapped 3443e9c3 (webmd.com, hard) out for 95cad96f (rottentomatoes.com, easy) at the author's request; rottentomatoes verified reachable through the proxy. Swapped 4e801ba1 (thumbtack.com, medium) out for dd44c665 (spothero.com, hard) at the author's request on cost grounds; spothero verified reachable through the proxy. Seven completed thumbtack runs (4 successes) are archived, not counted. Swapped c94551d2 (petfinder.com, 3-filter search) out for b6d10e9b (foxsports.com) on cost grounds: all 4 arms run failed it at 17-29 min each, so it produced no discordant pairs. foxsports verified reachable through the proxy.
