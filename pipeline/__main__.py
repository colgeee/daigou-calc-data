import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pipeline", description="Build Daigou Calc data files.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rates", help="build out/v1/rates.json.gz")
    r.add_argument("--out", default="out")
    r.add_argument(
        "--states", default="", help="comma-separated state codes to limit the build (dev only)"
    )
    f = sub.add_parser("fx", help="build out/v1/fx.json")
    f.add_argument("--out", default="out")
    args = p.parse_args(argv)
    if args.cmd == "rates":
        from pipeline.build_rates import main as run
        return run(args.out, [s for s in args.states.upper().split(",") if s])
    from pipeline.build_fx import main as run
    return run(args.out)


if __name__ == "__main__":
    sys.exit(main())
