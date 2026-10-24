"""Headway: a reliability lab for Prague public transport.

Headway archives real-time PID vehicle positions published by the Golemio platform,
reconstructs stop-level arrival and departure events, audits the quality of the source
data, measures route reliability and delay propagation, and nowcasts downstream delays
with calibrated uncertainty. Each subpackage implements one layer of the pipeline, and
layers exchange data only through files with fixed schemas.
"""
