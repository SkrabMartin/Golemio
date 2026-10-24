"""Layer L5: chronological split, baselines, nowcast models and evaluation.

Defines a chronological train, validation and locked holdout split in code, builds
prediction examples from data available strictly up to the prediction time, and evaluates
simple baselines against per-horizon models with split-conformal prediction intervals.
Transfer-miss probabilities are derived from the predictive distributions and checked for
calibration. The holdout is evaluated once, following a protocol fixed in advance.
"""
