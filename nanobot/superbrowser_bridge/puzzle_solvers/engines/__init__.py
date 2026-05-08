"""Reasoning engines for puzzle solvers.

Each engine is optional — imports are lazy so the module loads on a host
that's missing a specific engine's dependencies. Solvers should try/except
around the import and degrade gracefully (emit a clear error action rather
than crash the solve loop).
"""
