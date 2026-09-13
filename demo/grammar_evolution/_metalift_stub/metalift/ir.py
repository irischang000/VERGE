"""Stub -- see _stub_object.py. Every name resolves to a permissive Stub()."""

from ._stub_object import Stub

_stub = Stub()


def __getattr__(name):  # PEP 562: catches every `from metalift.ir import <anything>`
    return _stub
