# e9_evaluator_validation — human validation of the automatic evaluators

**Question.** How often do WebJudge (primary), the final-answer judge (secondary) and the protocol's
combined decision agree with two independent human annotators, and what are the false-positive /
false-negative patterns?

**Procedure.**
1. `python -m eval.experiments.e9_evaluator_validation.make_sheet --experiments e1_main,e2_memory_policy --n 60`
   builds a stratified sample (automatic verdict × arm × site family) into
   `eval/artifacts/e9_evaluator_validation/labelling_sheet.csv` with task, final answer, action tail and the
   last three screenshot paths. Hide the `auto_*` columns before labelling.
2. Two annotators fill `label_h1` / `label_h2` (`success` | `failure` | `cannot_judge`) independently.
3. `python -m eval.experiments.e9_evaluator_validation.analyze` reports raw agreement and Cohen's κ for
   human–human and human–each evaluator (on items where the humans agree), confusion matrices
   (FP = automatic success / human failure) and the disagreement list (`agreement.json`, `agreement.tex`).

Deterministic checks (`eval/benchmarks/checks.json`) are treated as ground truth where defined and the
evaluators' behaviour against them is reported separately.
