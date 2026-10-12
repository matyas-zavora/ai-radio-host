"""Cloud Function: unlink billing from the project once the budget is reached.

Triggered by the budget's Pub/Sub notifications. Without billing every paid API in the
project stops, so Google TTS can never cost more than the budget (plus reporting delay).
Re-enable by linking the billing account again in the console.
"""

import base64
import json
import os

import functions_framework
from google.cloud import billing_v1

PROJECT = os.environ["TARGET_PROJECT"]
# Cut billing once the reported cost exceeds this (0 = at the first real charge).
# Free-tier usage is reported as 0, so it never triggers this.
MAX_COST = float(os.environ.get("MAX_COST", "0"))


@functions_framework.cloud_event
def stop_billing(cloud_event):
    msg = json.loads(base64.b64decode(cloud_event.data["message"]["data"]))
    cost, budget = float(msg.get("costAmount", 0)), float(msg.get("budgetAmount", 0))
    print(f"budget update: cost {cost} of {budget} {msg.get('currencyCode', '')}")
    if cost <= MAX_COST:
        return

    name = f"projects/{PROJECT}"
    if msg.get("test"):
        print(f"TEST message: would unlink billing from {name}")
        return

    client = billing_v1.CloudBillingClient()
    if not client.get_project_billing_info(name=name).billing_enabled:
        print("billing already disabled")
        return
    client.update_project_billing_info(
        name=name, project_billing_info=billing_v1.ProjectBillingInfo(billing_account_name=""))
    print(f"BUDGET REACHED: billing unlinked from {name}")
