from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentForge adversarial MVP")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Run a campaign against the target")
    run_p.add_argument("--cases", default="evals/cases")
    run_p.add_argument(
        "--mutate",
        action="store_true",
        help="Also synthesize LLM-mutated variants of the first seed",
    )
    run_p.add_argument(
        "--live",
        action="store_true",
        help="Hit the deployed Co-Pilot (requires OAuth). Default uses MockCopilotClient.",
    )

    sub.add_parser("init-db", help="Apply migrations/001_initial.sql to Postgres")
    sub.add_parser("version", help="Print version and exit")
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

    if args.cmd == "run":
        import asyncio
        from pathlib import Path

        from agentforge_adversarial.config import Config
        from agentforge_adversarial.runner import run_campaign

        campaign_id = asyncio.run(
            run_campaign(
                Config.from_env(),
                Path(args.cases),
                mutate=args.mutate,
                use_mock=not args.live,
            )
        )
        print(f"\nCampaign {campaign_id} complete. Dashboard: streamlit run dashboard/app.py")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
