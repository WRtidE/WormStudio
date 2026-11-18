"""Host-OS boundaries. Only this package may test the host OS.

Every other module asks for a capability (``is_darwin``, ``is_windows``,
``selected_device``) instead of branching on ``sys.platform``. The rule is
enforced by ``tests/test_platform_boundary.py``; see docs/ARCHITECTURE.md.
"""
