"""One-line leaderboard summary for the CI job summary. Exits non-zero if there is none."""

import json
import sys

import points_core as core


def main():
    try:
        with open(core.META_FILE) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return 1
    total = m.get("total_supplied_usd") or 0
    print(f"- Market: `{m.get('market')}`")
    print(f"- Last run: {m.get('last_run_utc')}")
    print(f"- Addresses: {m.get('addresses')}")
    print(f"- Total supplied: ${total:,.2f}")
    print(f"- Points basis: `{m.get('points_basis')}`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
