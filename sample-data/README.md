# sample-data — Cross-skill test fixtures

Shared test data used by multiple skills. Lives at repo root because it is **not owned by any single skill** but referenced by several.

## Available datasets

### `erp/`

Cosmetics manufacturer ERP fixture. ~40K rows across 5 tables:
- `production_orders` — production schedules
- `quality_inspections` — quality test results (with realistic dirty data — nulls, future dates, outliers)
- `suppliers` — supplier master
- `products` — SKU catalog
- `inventory_movements` — stock changes

Used by:
- [`data-platform-pipeline-skill`](../data-platform-pipeline-skill/) — as `source_type=s3` input
- [`data-platform-consumption-skill`](../data-platform-consumption-skill/) — for downstream dashboard / chat-agent demos

Regenerate (Python 3 + pandas):

```bash
cd sample-data/erp
python3 generate_data.py
# Outputs: production_orders.csv, quality_inspections.csv, suppliers.csv,
#          products.csv, inventory_movements.csv
```

## Conventions for new shared datasets

If a new dataset is shared by 2+ skills, add it under `sample-data/<dataset-name>/` with:

1. `README.md` describing the dataset (tables, row counts, intentional dirty data)
2. A regeneration script (Python preferred) so the data can be reproduced
3. A pointer in each consuming skill's README to the dataset path

If a dataset is consumed by a single skill only, keep it under `<skill>/sample-data/` instead.
