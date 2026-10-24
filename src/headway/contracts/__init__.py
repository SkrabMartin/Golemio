"""Typed contracts for external data sources.

Each module describes one versioned source format, written against recorded responses
rather than assumptions. Contracts validate raw bytes as received, accept fields that are
not yet modelled, and report them, so that upstream format changes become visible instead
of being silently dropped.
"""
