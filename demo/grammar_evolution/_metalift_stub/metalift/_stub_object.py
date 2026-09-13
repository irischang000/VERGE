"""A permissive placeholder that accepts any attribute access, call, or
operator and returns itself.

Only exists so that `exec(code, namespace)` succeeds when LEVI defines an
evolved `inv_grammar` function in its own Python 3.11 process (which has no
real `metalift` installed) -- it never needs to behave correctly, because
the function body is never actually *called* there. It's only called for
real in metalift's own Python 3.10 subprocess (verge_grammar_bridge.py),
where the genuine `metalift.ir`/`metalift.vc_util` are installed and this
stub is never imported at all.
"""


class Stub:
    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return self

    def __getitem__(self, item):
        return self

    def __add__(self, other):
        return self

    __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = __add__
    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = __call__
    __and__ = __or__ = __rand__ = __ror__ = __add__
