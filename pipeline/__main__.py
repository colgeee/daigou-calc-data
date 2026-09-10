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
    b = sub.add_parser("bounds", help="build out/v1/bounds.json.gz")
    b.add_argument("--out", default="out")
    b.add_argument(
        "--sources",
        default="cdtfa,wa-dor,tiger,gu",
        help="comma-separated sources to build (dev only); the floors narrow to match",
    )
    d = sub.add_parser("diff-rates", help="compare out/v1/rates.json against the published one")
    d.add_argument("--out", default="out")
    d.add_argument(
        "--published",
        required=True,
        help="the published rates file: a path, an http(s) URL, or git:<rev>:<path>",
    )
    d.add_argument(
        "--allow-swings", action="store_true",
        help="publish anyway, with the swings printed on the record",
    )
    args = p.parse_args(argv)
    # Every builder is imported inside its branch, the way `rates` already was, so `--help`
    # never pays for a source module's imports.
    if args.cmd == "bounds":
        from pipeline.bounds.build import main as run_bounds
        return run_bounds(args.out, [s.strip() for s in args.sources.split(",") if s.strip()])
    if args.cmd == "diff-rates":
        from pipeline.diff_rates import main as run_diff
        return run_diff(args.out, args.published, allow_swings=args.allow_swings)
    from pipeline.build_rates import main as run
    return run(args.out, [s for s in args.states.upper().split(",") if s])


if __name__ == "__main__":
    sys.exit(main())
