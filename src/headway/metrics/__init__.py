"""Layer L4: reliability metrics and delay propagation.

Computes headways and reliability metrics per route, direction, stop and time of day:
on-time share, headway coefficient of variation, excess waiting time and bunching rate,
each with bootstrap confidence intervals and sample sizes. It also estimates how much
delay each segment adds or absorbs, using a mixed-effects model over segment, hour and
day type.
"""
