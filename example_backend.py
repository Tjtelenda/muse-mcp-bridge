"""Example household backend for the muse MCP bridge.

The production bridge talks to a private household dashboard service.
This example implements the same tiny interface with sample data so the
server runs standalone:

    MUSE_MCP_BACKEND_MODULE=example_backend

A real backend only needs a ``DashboardService`` class with:

- ``list_todos(user, include_all=False)`` -> list of dicts with at least
  ``id``, ``title``, ``status``, ``priority``, ``due_at``, ``category``
  and a ``completed`` flag.
- ``chores(user)`` -> list of dicts with at least ``id``, ``title``,
  ``completed_current_interval``, ``skipped_current_interval`` and
  ``next_due_at``.
"""


class DashboardService:
    def list_todos(self, user, include_all=False):
        return [
            {"id": 1, "title": "Water the plants", "status": "open",
             "priority": "normal", "due_at": None, "category": "home",
             "completed": False},
            {"id": 2, "title": "Book the dentist", "status": "open",
             "priority": "high", "due_at": None, "category": "personal",
             "completed": False},
            {"id": 3, "title": "Take out recycling", "status": "done",
             "priority": "normal", "due_at": None, "category": "home",
             "completed": True},
        ]

    def chores(self, user):
        return [
            {"id": 1, "title": "Clean the litter tray",
             "completed_current_interval": False,
             "skipped_current_interval": False, "next_due_at": None},
            {"id": 2, "title": "Mop the kitchen",
             "completed_current_interval": True,
             "skipped_current_interval": False, "next_due_at": None},
        ]
