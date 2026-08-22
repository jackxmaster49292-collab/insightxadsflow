"""Repository layer.

Every method that reaches a customer-owned object takes ``user_id`` as a
required keyword argument. There is deliberately no overload that omits it —
that is what makes cross-user access structurally impossible rather than merely
checked in a handler someone might forget. See docs/SECURITY.md §5.
"""
