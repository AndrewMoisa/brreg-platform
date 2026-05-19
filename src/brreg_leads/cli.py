import argparse
import logging
import sys
from datetime import date

from . import ingest


def _cmd_ingest(args: argparse.Namespace) -> int:
    summary = ingest.run_ingest(
        since=args.since,
        until=args.until,
        kommuner=args.kommune or None,
        skip_oppdateringer=args.skip_oppdateringer,
    )
    print(
        f"new_as_seen={summary.new_as_seen} "
        f"updates_seen={summary.updates_seen} "
        f"roller_fetched={summary.roller_fetched} "
        f"leads_upserted={summary.leads_upserted} "
        f"enk_conversions={summary.enk_conversions}"
    )
    return 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    if not args.since:
        print("--since YYYY-MM-DD is required for backfill", file=sys.stderr)
        return 2
    # Validate
    date.fromisoformat(args.since)
    return _cmd_ingest(args)


def _cmd_seed_enk(args: argparse.Namespace) -> int:
    count = ingest.seed_enk(kommuner=args.kommune or None)
    print(f"seeded_enk={count}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run(
        "brreg_leads.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brreg-leads")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("ingest", help="Run daily ingest (uses stored cursor or last 30 days)")
    pi.add_argument("--since", help="ISO date override; defaults to stored cursor")
    pi.add_argument("--until", help="ISO date upper bound (optional)")
    pi.add_argument("--kommune", action="append", help="Override kommunenummer (repeatable)")
    pi.add_argument("--skip-oppdateringer", action="store_true", help="Skip change-feed pull")
    pi.set_defaults(func=_cmd_ingest)

    pb = sub.add_parser("backfill", help="One-shot ingest from a specific date")
    pb.add_argument("--since", required=True, help="ISO date, e.g. 2026-04-01")
    pb.add_argument("--until", help="ISO date upper bound (optional)")
    pb.add_argument("--kommune", action="append", help="Override kommunenummer (repeatable)")
    pb.add_argument("--skip-oppdateringer", action="store_true")
    pb.set_defaults(func=_cmd_backfill)

    pe = sub.add_parser(
        "seed-enk",
        help="One-shot seed of active ENKs in target kommuner (run once before relying on ENK→AS detection)",
    )
    pe.add_argument("--kommune", action="append", help="Override kommunenummer (repeatable)")
    pe.set_defaults(func=_cmd_seed_enk)

    ps = sub.add_parser("serve", help="Run the local dashboard")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8000)
    ps.add_argument("--reload", action="store_true")
    ps.set_defaults(func=_cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
