import json
from pathlib import Path

path = Path(r"B:\Finanças\Extratos\fluxo-backup-latest.json")
with open(path, "r", encoding="utf-8") as f:
    s = json.load(f)

accounts = {a["id"] for a in s["accounts"]}
categories = {c["id"] for c in s["categories"]}
cards = {c["id"] for c in s["cards"]}
loans = {l["id"] for l in s["loans"]}

errors = []
for t in s["transactions"]:
    tid = t["id"]
    if t["accountId"] not in accounts:
        errors.append(f"Trans {tid}: accountId {t['accountId']} not in accounts")
    if t["type"] == "TRANSFER":
        if not t.get("toAccountId") or t["toAccountId"] not in accounts or t["toAccountId"] == t["accountId"]:
            errors.append(f"Trans {tid}: invalid transfer toAccountId {t.get('toAccountId')}")
    else:
        if not t.get("categoryId") or t["categoryId"] not in categories:
            errors.append(f"Trans {tid}: invalid categoryId {t.get('categoryId')}")
    if t["amountCents"] <= 0:
        errors.append(f"Trans {tid}: non-positive amount {t['amountCents']}")

for l in s["loans"]:
    lid = l["id"]
    if l.get("accountId") and l["accountId"] not in accounts:
        errors.append(f"Loan {lid}: account {l['accountId']} not found")
    if l.get("categoryId") and l["categoryId"] not in categories:
        errors.append(f"Loan {lid}: category {l['categoryId']} not found")

for i in s["loanInstallments"]:
    iid = i["id"]
    if i["loanId"] not in loans:
        errors.append(f"Installment {iid}: loan {i['loanId']} not found")

print(f"Validation complete! Total errors found: {len(errors)}")
if errors:
    for e in errors[:10]:
        print("  ERROR:", e)
else:
    print(">>> SUCCESS: 100% VALID FLUXO BACKUP SNAPSHOT! <<<")
