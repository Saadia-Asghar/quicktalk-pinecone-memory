"""Build precomputed customer profiles and warm the configured cache."""

from __future__ import annotations

import json

from dotenv import load_dotenv

from analytics import AnalyticsRepository


def main() -> None:
    load_dotenv(override=True)
    repository = AnalyticsRepository()
    customers = repository.backfill_profiles()
    warmup = repository.warm()
    print(json.dumps({"status": "PASS", "profiles_built": customers, **warmup}, indent=2))


if __name__ == "__main__":
    main()
