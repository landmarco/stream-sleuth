"""Local audio fingerprinting for stream-sleuth.

"Shazam for your own library": spectral peaks become combinatorial hashes
(Wang 2003, An Industrial-Strength Audio Search Algorithm) stored per track,
and a query clip is matched by offset-consistent hash voting. Hash-table
layout follows dejavu (MIT, worldveil/dejavu); a quad-hashing mode
(Sonnleitner & Widmer 2016, Robust Quad-Based Audio Fingerprinting) is
available experimentally via SLEUTH_HASH_MODE=quad.
"""
