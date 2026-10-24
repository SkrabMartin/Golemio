"""Synthetic archive generator with known ground truth.

Generates a simulated network, its GTFS timetable versions and weeks of raw vehicle
position polls in exactly the collector's file format, plus truth tables (true stop
events, planted segment effects, planted data defects). Every downstream layer can run on
it unchanged, and its results can be checked against the truth. Output is synthetic and
labelled as such; it says nothing about the real network.
"""

from headway.synth.archive import SynthResult, generate
from headway.synth.config import SynthConfig, load_synth_config

__all__ = ["SynthConfig", "SynthResult", "generate", "load_synth_config"]
