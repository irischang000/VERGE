"""Stub -- see _stub_object.py. Every name resolves to a permissive Stub()."""

from ._stub_object import Stub

_stub = Stub()


def __getattr__(name):
    return _stub
