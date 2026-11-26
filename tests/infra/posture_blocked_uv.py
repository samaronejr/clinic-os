"""Deliberately stalled *test-only* launcher and descendant for timeout cleanup."""

import os
import signal

if __name__ == "__main__":
    # Both actors remain in Make's owned group. Neither reads the environment,
    # opens a database, or emits argv. The supervisor must reap both.
    os.fork()
    signal.pause()
