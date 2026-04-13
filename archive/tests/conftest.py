import sys
import os
import pytest

# Make /opt/mnemos importable as the root package path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")
