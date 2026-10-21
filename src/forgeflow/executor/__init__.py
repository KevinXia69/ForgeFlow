"""Root-side executor primitives: protocol, peer credentials, bootstrap checks.

These modules are imported only by the executor daemon (root) and by tests.
The agent-side client lives in ``forgeflow.recovery.client`` and must never
import the bootstrap or the write key reader.
"""
