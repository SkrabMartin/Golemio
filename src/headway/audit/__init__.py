"""Layer L2: data-quality audit.

Reports what is wrong with the collected data, without changing it: gaps and errors of the
collector, stale or implausible positions, positions off the route, one vehicle on two
trips, silent trips and trips unknown to the timetable. Every threshold comes from
``config/audit.toml`` and is printed in the report.
"""
