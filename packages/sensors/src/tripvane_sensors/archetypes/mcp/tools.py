"""The MCP sensor's tools: the standard decoy tools plus two bookkeeping lookups.

Like every decoy tool, list_customers and get_invoice do no I/O; their results are
static text about fictional customers. Phone numbers are in the 555-01xx range, which
is reserved for fiction.
"""

from typing import Any

from tripvane_sensors.runtime.tools import STANDARD_TOOLS, DecoyTool, FakeResult

_CUSTOMERS = """\
ID        NAME                         CONTACT          PHONE            BALANCE
C-10482   Harbor & Pine Bakery         Amara Okafor     +1 555 0142      $1,240.00
C-10517   Ridgeway Bicycle Repair      Tomas Lindqvist  +1 555 0178      $0.00
C-10533   Fernhill Dental Studio       Priya Raman      +1 555 0115      $3,815.50
C-10561   Blue Heron Landscaping       Dale Whitcombe   +1 555 0193      $642.75
4 of 312 customers shown
"""

_INVOICE = """\
Invoice INV-2026-0418
Customer: C-10482 Harbor & Pine Bakery
Issued: 2026-09-02   Due: 2026-10-02   Status: open
  Monthly bookkeeping, September 2026       $980.00
  Payroll export add-on                     $260.00
Total due: $1,240.00
"""


def _static(text: str) -> FakeResult:
    def fake_result(arguments: dict[str, Any]) -> str:
        return text

    return fake_result


_STRING = {"type": "string"}

LIST_CUSTOMERS = DecoyTool(
    name="list_customers",
    description="List customer accounts in Quillstone Ledger with contact details and balance.",
    input_schema={
        "type": "object",
        "properties": {"query": _STRING, "limit": {"type": "integer"}},
        "required": [],
    },
    fake_result=_static(_CUSTOMERS),
)

GET_INVOICE = DecoyTool(
    name="get_invoice",
    description="Fetch one invoice from Quillstone Ledger by its invoice id.",
    input_schema={
        "type": "object",
        "properties": {"invoice_id": _STRING},
        "required": ["invoice_id"],
    },
    fake_result=_static(_INVOICE),
)

MCP_TOOLS: tuple[DecoyTool, ...] = (*STANDARD_TOOLS, LIST_CUSTOMERS, GET_INVOICE)
