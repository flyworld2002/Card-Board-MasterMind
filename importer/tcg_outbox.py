"""
importer/tcg_outbox.py
Outbound side of TCGplayer seller messages (plan: docs/plans/tcg-seller-messages.md, Phase 3).

export_approved() will write each approved message as outbox/msg_<id>.json for Grok to send.
It is called by `main.py --tcg-messages-export`, which the desktop API's Approve button runs.

PHASE 2 STATE: there is no message outbox table yet (it arrives with Phase 3), so this only reports
that there is nothing to send. The command and the API endpoint exist already so that adding Phase 3
needs no restart of the desktop program.
"""


def export_approved(dry_run: bool = False) -> dict:
    return {"exported": 0, "note": "nothing to send (sending is built in Phase 3)"}
