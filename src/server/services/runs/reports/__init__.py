"""Report-back subsystem: background runs delivering results as turns.

``subagent.py`` owns the ``task_report_back`` executor (a Task/RunWorkflow
child reporting to the thread that launched it, on the Watchdog lane);
``steer_executor.py`` posts a steer its live turn never read as the next
turn, and ``steer.py``/``task_steer.py`` let a live turn take both in.
The Chief of Staff's ``handoff/`` pipeline and its ``report_back`` hook type
were retired with direct chat (RA-3); no executor is registered for it.
"""
