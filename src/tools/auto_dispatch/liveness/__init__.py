"""N76 dispatch-liveness policy, owner, consistency and record layer.

The state-machine wiring and the operator recovery verb will import from this
package. Everything here is pure: the recovery table is data, the owner
decision is a function of a line snapshot and a caller-supplied clock, and a
liveness record is a plain value stored under ``gates.lines[id].liveness``.
No module in this package starts I/O or sleeps.

Submodules are imported explicitly (``liveness.record``, ``liveness.modes``,
...) so importing just one of them never pulls in the others.
"""
