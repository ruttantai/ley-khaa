"""The seed summary_stats workflow (spec §5.6). Hand-written, not model-written."""

SOURCE = '''"""Count, min, max and mean of every numeric column in one dataset.

A seed workflow of ley-khaa's registry: proven code, run without a model.
"""
import csv
import json

with open("inputs/params.json", encoding="utf-8") as handle:
    params = json.load(handle)

TARGET = params["output"]

with open("inputs/" + params["inputs"]["dataset"], newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))

summary = []
for field in (list(rows[0].keys()) if rows else []):
    values = []
    for row in rows:
        try:
            values.append(float(row.get(field, "")))
        except (TypeError, ValueError):
            continue
    if not values:
        continue
    summary.append({
        "column": field,
        "count": str(len(values)),
        "min": "%.4f" % min(values),
        "max": "%.4f" % max(values),
        "mean": "%.4f" % (sum(values) / len(values)),
    })

fields = ["column", "count", "min", "max", "mean"]
if TARGET.endswith(".xlsx"):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "result"
    sheet.append(fields)
    for row in summary:
        sheet.append([row[field] for field in fields])
    book.save(TARGET)
else:
    with open(TARGET, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\\n")
        writer.writeheader()
        for row in summary:
            writer.writerow(row)

print("summarised %d numeric column(s) over %d row(s)" % (len(summary), len(rows)))
'''

WORKFLOW = {
    "name": "summary_stats",
    "description": "count, min, max and mean of every numeric column of one dataset",
    "output_format": "csv",
    "operation_aliases": ["summary_stats"],
    "inputs": [{"role": "dataset", "suffixes": [".csv"]}],
    "source": SOURCE,
}
