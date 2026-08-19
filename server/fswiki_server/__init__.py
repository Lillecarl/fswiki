"""The wiki, for people who are not running a client.

A browser talks to this; the CLI and the FUSE mount talk to PostgREST
directly. It is not a proxy in front of them, and it holds no identity of its
own: a visitor's token is passed through, and Postgres decides what comes back.
"""

__all__ = ["config", "migrate"]
