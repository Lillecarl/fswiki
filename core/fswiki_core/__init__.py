"""Shared between the FUSE client and the CLI: PostgREST access and naming.

Deliberately free of pyfuse3 and libfuse, so publishing a draft from a server or
a CI job does not require the ability to mount anything.
"""

__version__ = "0.1.0"
