from pathlib import Path

import environ

environ.Env.read_env(Path(__file__).resolve().parents[2] / ".env")

from .base import *  # noqa: E402, F403

DEBUG = True
ROOT_URLCONF = "config.urls_dev"
