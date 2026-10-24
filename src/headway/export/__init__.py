"""Layer L6: export of aggregates for the website, exploration app and report.

Turns metrics and findings into small, documented static files (segment geometries with
delay increments, route reliability tables, transfer risks and dataset metadata) consumed
by the static web map, the Streamlit exploration app and the generated report. Exports
contain only precomputed historical aggregates, never live data.
"""
