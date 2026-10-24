"""Static GTFS snapshots, version registry and time conversion.

Archives daily snapshots of the PID static GTFS feed keyed by content hash, maintains a
registry that maps every service date to the exact feed version valid on that date, and
loads the required tables with explicit types. It also owns the single, tested function
that converts GTFS schedule times (including times past 24:00) to UTC across daylight
saving changes.
"""
