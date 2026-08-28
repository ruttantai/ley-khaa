"""The seed set_difference workflow (spec §5.6).

Hand-written, not model-written: this is what a hardened, promoted capability is
supposed to look like, and it is the thing a promoted script is measured against.
Reads its binding from inputs/params.json like every other generator.
"""

SOURCE = '''"""Rows in the left input whose key is absent from the right input.

A seed workflow of ley-khaa's registry: proven code, run without a model.
"""
import csv
import json

with open("inputs/params.json", encoding="utf-8") as handle:
    params = json.load(handle)

TARGET = params["output"]


def read_rows(name):
    with open("inputs/" + name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


left = read_rows(params["inputs"]["left"])
right = read_rows(params["inputs"]["right"])

fields = list(left[0].keys()) if left else ["ticker"]
key = fields[0]
seen = {row.get(key) for row in right}
missing = [row for row in left if row.get(key) not in seen]

if TARGET.endswith(".xlsx"):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "result"
    sheet.append(fields)
    for row in missing:
        sheet.append([row.get(field, "") for field in fields])
    book.save(TARGET)
else:
    with open(TARGET, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\\n")
        writer.writeheader()
        for row in missing:
            writer.writerow({field: row.get(field, "") for field in fields})

print("%d of %d rows keyed on %s are missing from the second input"
      % (len(missing), len(left), key))
'''

WORKFLOW = {
    "name": "set_difference",
    "description": "rows in the first input whose key column is absent from the second",
    "operation_aliases": ["set_difference"],
    # The golden universe check asks for Excel, and a workflow declares exactly
    # one output format — a CSV request is a different capability, not this one.
    "output_format": "xlsx",
    "inputs": [
        {"role": "left", "suffixes": [".csv"]},
        {"role": "right", "suffixes": [".csv"]},
    ],
    "source": SOURCE,
}
