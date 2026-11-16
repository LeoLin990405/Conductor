"""Ticket-side services for the common collaboration protocol (N80 L2).

``tools.invoke`` is the one pipeline every ``/api/v1/collab`` tool call takes
(loopback, mode, app-token auth, scope, rate limit, schema, payload secrets,
handler, redaction, audit). The handler modules register themselves with it:

* ``tickets``  task_list, task_get, task_create (proposals);
* ``leases``   task_claim, task_heartbeat, task_release;
* ``report``   task_report (completion contract + system facts);
* ``handoff``  handoff_request, handoff_list, handoff_accept;
* ``messages`` agent_ask, agent_inbox;
* ``reviews``  review_request, review_list, review_submit;
* ``apps``     whoami and the operator's app registration actions.

``sweep.CollabLeaseSweeper`` expires stale leases and returns their Tickets
to ``queued``. Import the submodules directly; this package stays light.
"""

__all__ = [
    "apps",
    "audit",
    "auth",
    "handoff",
    "leases",
    "messages",
    "report",
    "reviews",
    "sweep",
    "tickets",
    "tools",
]
