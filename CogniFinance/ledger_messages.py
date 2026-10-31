"""
ledger_messages.py ver 0.1
For CogniFinance ver 0.9.67

The ledger's refusal messages are written for Pro Mode ("Reopen the
period on the Reconciliation tab"). Simple Mode hides that tab, so the
same refusal is shown with advice that has a way forward. The ledger's
own wording is untouched; only the remedy sentence is swapped.
"""

LOCK_ADVICE_PRO = "Reopen the period on the Reconciliation tab first."
LOCK_ADVICE_SIMPLE = ("This period was locked in Pro Mode. Turn on "
                      "\"Show advanced screens\" in Settings to reopen it.")

RECONCILED_PRO = "Cannot edit a reconciled transaction. Unreconcile it first."
RECONCILED_SIMPLE = ("This transaction was reconciled in Pro Mode, so it can't be "
                     "changed here. Turn on \"Show advanced screens\" in Settings "
                     "to unreconcile it.")


def for_simple_mode(message: str) -> str:
    """The ledger's message, with Simple Mode advice where it applies."""
    return (message.replace(LOCK_ADVICE_PRO, LOCK_ADVICE_SIMPLE)
                   .replace(RECONCILED_PRO, RECONCILED_SIMPLE))
