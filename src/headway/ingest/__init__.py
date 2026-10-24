"""Layer L1: ingest of raw snapshots into a positions dataset.

Parses raw snapshots with a versioned parser into one row per vehicle observation,
assigns each row a service date and the GTFS version valid for it, deduplicates
observations repeated across polls while keeping provenance, and writes Parquet
partitioned by service date. Ingest is idempotent and can rebuild the dataset from the
raw archive at any time; records that fail validation are kept with a reason instead of
being dropped silently.
"""
