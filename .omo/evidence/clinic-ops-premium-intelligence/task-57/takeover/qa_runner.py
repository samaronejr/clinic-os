"""Run retained Manual-QA with the actual guarded database fixtures."""

import sys

import django
import pytest

django.setup()
import conftest

raise SystemExit(pytest.main(sys.argv[1:], plugins=[conftest]))
