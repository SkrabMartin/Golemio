"""Layer L3: stop-event reconstruction.

Projects vehicle positions onto the trip shape in a metric coordinate system to obtain
distance along the route, cleans the resulting time-distance series and interpolates
arrival and departure times at each stop, with an uncertainty estimate for every event.
Reconstructed delays are compared against the delay reported by the source, which serves
as a second measurement rather than ground truth.
"""
