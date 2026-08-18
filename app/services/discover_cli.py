"""Run badge discovery from the shell: python -m app.services.discover_cli

Useful for the first population of the collection, and for testing the Claude
calls without booting the web app.
"""

import argparse
import json
import sys

from app.services.badge_discovery import discover_badges, synchronize_badges


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover MongoDB skill badges.")
    parser.add_argument("--instructions", help="Extra guidance for the research pass.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what was found without writing to MongoDB.",
    )
    args = parser.parse_args()

    if args.dry_run:
        badges, notes = discover_badges(extra_instructions=args.instructions)
        print(f"Found {len(badges)} badge(s).\n", file=sys.stderr)
        print(json.dumps([b.model_dump() for b in badges], indent=2, default=str))
        print("\n--- research notes ---\n" + notes, file=sys.stderr)
        return 0

    summary = synchronize_badges(extra_instructions=args.instructions)
    print(f"Found {summary['discovered']} badge(s).\n", file=sys.stderr)
    print(json.dumps(summary["badges"], indent=2, default=str))
    print(
        f"\nrun {summary['run_id']}: {summary['inserted']} inserted, "
        f"{summary['modified']} updated, {len(summary['merged'])} recognised as "
        f"existing badges.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
