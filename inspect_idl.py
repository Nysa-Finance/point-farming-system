import json

with open("klend_idl.json") as f:
    idl = json.load(f)

def print_account_fields(account_name):
    for acc in idl.get("accounts", []):
        if acc["name"] == account_name:
            print(f"\n=== {account_name} ===")
            # la struttura può variare leggermente a seconda della versione IDL (0.29 vs 0.30+)
            type_def = acc.get("type", {})
            fields = type_def.get("fields", [])
            for field in fields:
                print(f"  {field['name']}: {field['type']}")
            return
    print(f"Account '{account_name}' non trovato — controlla idl['types'] invece.")

def print_type_fields(type_name):
    for t in idl.get("types", []):
        if t["name"] == type_name:
            print(f"\n=== {type_name} ===")
            fields = t.get("type", {}).get("fields", [])
            for field in fields:
                print(f"  {field['name']}: {field['type']}")
            return
    print(f"Type '{type_name}' non trovato")

print_type_fields("ObligationCollateral")
print_type_fields("ObligationLiquidity")
print_type_fields("ReserveLiquidity")
print_type_fields("ReserveCollateral")
