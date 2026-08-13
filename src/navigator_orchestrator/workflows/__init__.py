"""Registered capabilities. R0 ships `echo` and nothing else.

Every module under here is scanned by the purity check (SPEC-AIP-002 AC-3):
nodes take `(state, deps)`, return a partial update, and never build a client
at import time.
"""
