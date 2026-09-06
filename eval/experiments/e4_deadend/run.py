from eval.core.experiment import run_main
from eval.experiments.e4_deadend import SPEC

main = run_main(SPEC)

if __name__ == "__main__":
    raise SystemExit(main())
