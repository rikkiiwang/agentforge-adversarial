from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentForge adversarial CLI")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Run a campaign against a target")
    run_p.add_argument("--cases", default="evals/cases")
    run_p.add_argument(
        "--no-mutate",
        action="store_true",
        help="Disable the Red Team mutator (run only the hand-curated seeds).",
    )
    run_p.add_argument(
        "--target",
        default=None,
        help="Name of a target row from the `targets` table. "
             "Defaults to the oldest row (typically the seeded Co-Pilot).",
    )
    run_p.add_argument(
        "--max-rounds",
        type=int,
        default=2,
        help="Max class-probe rounds after the initial dispatch. "
             "0 disables fan-out. Default 2.",
    )
    run_p.add_argument(
        "--mutations-per-seed",
        type=int,
        default=3,
        help="LLM mutations the Red Team mutator produces per seed "
             "(also used by PARTIAL re-entry). Default 3.",
    )

    sub.add_parser("init-db", help="Apply all SQL migrations in order")
    sub.add_parser("list-targets", help="Print all configured targets")
    sub.add_parser("version", help="Print version and exit")

    regress_p = sub.add_parser(
        "regress",
        help="Replay confirmed vulnerabilities against the current target "
             "and transition their state to fix_validated / reopened.",
    )
    regress_p.add_argument(
        "--target",
        help="Target name to replay against. Defaults to the seeded target.",
    )
    regress_p.add_argument(
        "--include-closed",
        action="store_true",
        help="Also replay vulns in state='closed' to catch re-FAILs that "
             "warrant reopening.",
    )
    regress_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be replayed without dispatching to the target "
             "or writing state transitions.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == "version":
        from agentforge_adversarial import __version__

        print(__version__)
        return 0

    if args.cmd == "init-db":
        import asyncio

        from agentforge_adversarial.config import Config
        from agentforge_adversarial.db import init_schema

        asyncio.run(init_schema(Config.from_env()))
        print("Schema applied.")
        return 0

    if args.cmd == "list-targets":
        import asyncio

        from agentforge_adversarial.config import Config
        from agentforge_adversarial.db import connection
        from agentforge_adversarial.targets import list_targets

        async def _go() -> None:
            cfg = Config.from_env()
            async with connection(cfg) as conn:
                rows = await list_targets(conn)
            for r in rows:
                print(f"  {r['name']:40} {r['target_type']:15} {r['target_url']}")

        asyncio.run(_go())
        return 0

    if args.cmd == "run":
        import asyncio
        from pathlib import Path

        from agentforge_adversarial.config import Config
        from agentforge_adversarial.runner import run_campaign

        campaign_id = asyncio.run(
            run_campaign(
                Config.from_env(),
                Path(args.cases),
                mutate=not args.no_mutate,
                mutations_per_seed=args.mutations_per_seed,
                target_name=args.target,
                max_rounds=args.max_rounds,
            )
        )
        print(f"\nCampaign {campaign_id} complete. Dashboard: streamlit run dashboard/app.py")
        return 0

    if args.cmd == "regress":
        import asyncio

        from agentforge_adversarial.config import Config
        from agentforge_adversarial.regression import regress_all

        async def _go() -> None:
            cfg = Config.from_env()
            results = await regress_all(
                cfg,
                target_name=args.target,
                include_closed=args.include_closed,
                dry_run=args.dry_run,
            )
            if not results:
                print(
                    "No vulnerabilities are due for regression "
                    "(no triaged/fix_proposed/reopened rows with stale "
                    "target_version). Try --include-closed to broaden."
                )
                return
            print(f"\nReplayed {len(results)} vulnerabilities"
                  f"{' (dry run)' if args.dry_run else ''}:\n")
            transitioned = {"fix_validated": 0, "reopened": 0, "no_change": 0}
            for r in results:
                state_marker = {
                    "fix_validated": "✅",
                    "reopened": "🔴",
                }.get(r.new_state, "·")
                print(
                    f"  {state_marker} [{r.new_verdict:7}] {r.case_id:40} "
                    f"{r.category:18} → {r.new_state}"
                )
                if r.new_state in transitioned:
                    transitioned[r.new_state] += 1
                else:
                    transitioned["no_change"] += 1
            print(
                f"\nSummary: ✅ fix_validated={transitioned['fix_validated']} · "
                f"🔴 reopened={transitioned['reopened']} · "
                f"unchanged={transitioned['no_change']}"
            )

        asyncio.run(_go())
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
