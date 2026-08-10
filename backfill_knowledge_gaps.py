"""Backfill strict missing-knowledge analytics for all locally imported chats."""

import json

from analytics import AnalyticsRepository


if __name__ == "__main__":
    print(json.dumps(AnalyticsRepository().backfill_knowledge_gaps(), indent=2))
