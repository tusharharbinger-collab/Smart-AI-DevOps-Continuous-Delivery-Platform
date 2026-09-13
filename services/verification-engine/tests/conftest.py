# services/verification-engine/tests/conftest.py
"""
Shared pytest fixtures and path configuration.
Adds services/verification-engine to sys.path so `from src.xyz import ...` works.
"""
import sys
import os

# Make the verification-engine root importable as a package root
engine_root = os.path.join(os.path.dirname(__file__), "..")
if engine_root not in sys.path:
    sys.path.insert(0, os.path.abspath(engine_root))
