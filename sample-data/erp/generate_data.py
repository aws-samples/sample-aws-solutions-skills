"""Generate realistic ERP sample data for cosmetics/consumer goods manufacturer.

Tables:
  - quality_inspections.csv   (~10,000 rows)
  - production_orders.csv     (~5,000 rows)
  - suppliers.csv             (30 rows)
  - products.csv              (20 rows)
  - inventory_movements.csv   (~20,000 rows)

Data is intentionally seeded with a small amount of dirty data
(nulls, future dates, outliers) so data-quality checks have something to find.
"""

import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

random.seed(42)

OUT_DIR = Path(__file__).parent

PRODUCTS = [f"PRD-{i:03d}" for i in range(1, 21)]
INSPECTORS = [f"EMP-{i:03d}" for i in range(1, 16)]
SUPPLIERS = [f"SUP-{i:03d}" for i in range(1, 31)]
LINES = ["LINE-A", "LINE-B", "LINE-C", "LINE-D"]
INSPECTION_TYPES = ["incoming_material", "in_process", "final_product", "packaging"]
DEFECT_TYPES = ["appearance", "weight", "color", "contamination", "packaging"]
SEVERITIES = ["minor", "major", "critical"]

# Per-product spec ranges (realistic measurement targets)
PRODUCT_SPECS = {}
for p in PRODUCTS:
    target = random.uniform(50, 500)
    tolerance = target * random.uniform(0.02, 0.08)
    PRODUCT_SPECS[p] = (target - tolerance, target + tolerance, target)

# Product unit of measure
PRODUCT_UOM = {p: random.choice(["kg", "L", "pcs"]) for p in PRODUCTS}

START_DATE = date(2024, 1, 1)
END_DATE = date(2025, 12, 31)
DATE_RANGE_DAYS = (END_DATE - START_DATE).days


def random_date(start=START_DATE, end=END_DATE):
    return start + timedelta(days=random.randint(0, (end - start).days))


def lot_number(d: date, seq: int) -> str:
    return f"LOT-{d.strftime('%Y%m')}-{seq:04d}"


# ---------------------------------------------------------------------------
# 1. production_orders.csv  — generate first so quality_inspections can reference real lots
# ---------------------------------------------------------------------------

PRODUCTION_ROWS = 5000
production_orders = []
lot_pool = []  # (lot_number, product_code, order_date) for cross-table reference

for order_id in range(1, PRODUCTION_ROWS + 1):
    product = random.choice(PRODUCTS)
    order_dt = random_date()
    lot_seq = random.randint(1, 9999)
    lot = lot_number(order_dt, lot_seq)

    planned = random.choice([500, 1000, 1500, 2000, 3000, 5000, 10000])
    # Most orders complete close to plan; some have low yield
    yield_rate = round(random.gauss(0.95, 0.04), 4)
    yield_rate = max(0.85, min(1.0, yield_rate))
    actual = int(planned * yield_rate)

    # Roughly 92% completed, 5% in_progress, 3% cancelled
    r = random.random()
    if r < 0.92:
        status = "completed"
    elif r < 0.97:
        status = "in_progress"
    else:
        status = "cancelled"

    # Production runs ~ 4–24 hours
    start_hour = random.randint(0, 23)
    start_min = random.choice([0, 15, 30, 45])
    start_dt = datetime.combine(order_dt, datetime.min.time()).replace(
        hour=start_hour, minute=start_min
    )
    duration_h = random.uniform(4, 24)
    end_dt = start_dt + timedelta(hours=duration_h)

    if status == "cancelled":
        actual = int(planned * random.uniform(0.0, 0.3))
        end_dt = start_dt + timedelta(hours=random.uniform(0.5, 4))
    elif status == "in_progress":
        actual = int(planned * random.uniform(0.3, 0.8))

    batch_size = random.choice([100, 250, 500, 1000])
    uom = PRODUCT_UOM[product]

    production_orders.append({
        "order_id": order_id,
        "lot_number": lot,
        "product_code": product,
        "order_date": order_dt.isoformat(),
        "planned_quantity": planned,
        "actual_quantity": actual,
        "unit_of_measure": uom,
        "production_line": random.choice(LINES),
        "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "batch_size": batch_size,
        "yield_rate": round(yield_rate, 4),
    })

    if status != "cancelled":
        lot_pool.append((lot, product, order_dt))

with open(OUT_DIR / "production_orders.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(production_orders[0].keys()))
    writer.writeheader()
    writer.writerows(production_orders)


# ---------------------------------------------------------------------------
# 2. quality_inspections.csv
# ---------------------------------------------------------------------------

INSPECTION_ROWS = 10000
inspections = []

NOTES_PASS = ["", "OK", "within spec", "no issues", "approved by QA"]
NOTES_FAIL = [
    "out of spec — rework",
    "rejected, returned to supplier",
    "deviation logged",
    "see attached lab report",
    "color drift observed",
    "moisture above limit",
]

for inspection_id in range(1, INSPECTION_ROWS + 1):
    # 70% link to a real production lot, 30% incoming material with synthetic lot
    if lot_pool and random.random() < 0.7:
        lot, product, order_dt = random.choice(lot_pool)
        inspect_dt = order_dt + timedelta(days=random.randint(0, 14))
        if inspect_dt > END_DATE:
            inspect_dt = END_DATE
    else:
        product = random.choice(PRODUCTS)
        inspect_dt = random_date()
        lot = lot_number(inspect_dt, random.randint(1, 9999))

    insp_type = random.choice(INSPECTION_TYPES)

    # 88% pass, 8% fail, 4% conditional
    r = random.random()
    if r < 0.88:
        result = "pass"
        defect_type = ""
        defect_severity = ""
    elif r < 0.96:
        result = "fail"
        defect_type = random.choice(DEFECT_TYPES)
        defect_severity = random.choices(
            SEVERITIES, weights=[0.5, 0.35, 0.15]
        )[0]
    else:
        result = "conditional"
        defect_type = random.choice(DEFECT_TYPES)
        defect_severity = "minor"

    spec_min, spec_max, target = PRODUCT_SPECS[product]
    if result == "pass":
        measurement = random.uniform(spec_min, spec_max)
    elif result == "conditional":
        # Borderline — within ~5% of bounds
        if random.random() < 0.5:
            measurement = spec_min - abs(spec_min) * random.uniform(0, 0.03)
        else:
            measurement = spec_max + abs(spec_max) * random.uniform(0, 0.03)
    else:  # fail
        if random.random() < 0.5:
            measurement = spec_min - abs(spec_min) * random.uniform(0.05, 0.25)
        else:
            measurement = spec_max + abs(spec_max) * random.uniform(0.05, 0.25)

    # Dirty data injection
    inspect_date_str = inspect_dt.isoformat()
    if random.random() < 0.02:                           # ~2% null inspection_date
        inspect_date_str = ""
    elif random.random() < 0.0008:                       # rare future dates (~8 rows)
        future = date(2026, random.randint(1, 12), random.randint(1, 28))
        inspect_date_str = future.isoformat()

    measurement_str = f"{measurement:.3f}"
    if random.random() < 0.05:                           # ~5% null measurement_value
        measurement_str = ""
    elif random.random() < 0.001:                        # rare extreme outliers
        measurement_str = f"{measurement * random.choice([100, -50]):.3f}"

    notes = random.choice(NOTES_PASS if result == "pass" else NOTES_FAIL)
    if random.random() < 0.4:                            # 40% of notes are null
        notes = ""

    inspections.append({
        "inspection_id": inspection_id,
        "lot_number": lot,
        "product_code": product,
        "inspection_date": inspect_date_str,
        "inspection_type": insp_type,
        "inspector_id": random.choice(INSPECTORS),
        "result": result,
        "defect_type": defect_type,
        "defect_severity": defect_severity,
        "measurement_value": measurement_str,
        "specification_min": f"{spec_min:.3f}",
        "specification_max": f"{spec_max:.3f}",
        "supplier_id": random.choice(SUPPLIERS),
        "notes": notes,
    })

with open(OUT_DIR / "quality_inspections.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(inspections[0].keys()))
    writer.writeheader()
    writer.writerows(inspections)


# ---------------------------------------------------------------------------
# 3. suppliers.csv
# ---------------------------------------------------------------------------

SUPPLIER_NAMES = [
    "Hansol Chemical Co., Ltd.",
    "Kolmar Korea",
    "Cosmax Materials",
    "LG H&H Ingredients",
    "Amorepacific Raw Materials",
    "BASF Personal Care",
    "Croda Korea",
    "Lubrizol Asia",
    "Symrise Asia Pacific",
    "Givaudan Korea",
    "Firmenich Seoul",
    "DSM Nutritional Products",
    "Evonik Industries Korea",
    "Clariant Korea",
    "Ashland Specialty Korea",
    "Dow Chemical Korea",
    "Wacker Chemicals Korea",
    "Shin-Etsu Silicones",
    "Innospec Korea",
    "Seppic Asia",
    "Jeen International",
    "Daejong Packaging",
    "Hyundai Plastics",
    "Samhwa Crown",
    "Pumtech Korea",
    "Yonwoo Co., Ltd.",
    "HCP Packaging Korea",
    "Albea Korea",
    "Berry Global Korea",
    "GlobalPak Industries",
]

SUPPLIER_TYPES = ["raw_material", "raw_material", "raw_material", "fragrance",
                  "packaging", "packaging", "equipment", "service"]
COUNTRIES = ["KR", "KR", "KR", "KR", "KR", "DE", "JP", "FR", "US", "CN"]

suppliers_rows = []
for i, name in enumerate(SUPPLIER_NAMES, start=1):
    sid = f"SUP-{i:03d}"
    s_type = random.choice(SUPPLIER_TYPES)
    country = random.choice(COUNTRIES)
    contract_start = date(
        random.randint(2015, 2024),
        random.randint(1, 12),
        random.randint(1, 28),
    )
    quality_rating = round(random.uniform(3.0, 5.0), 2)
    otd = round(random.uniform(0.80, 1.00), 4)

    r = random.random()
    if r < 0.85:
        status = "active"
    elif r < 0.95:
        status = "inactive"
    else:
        status = "suspended"

    safe = name.lower().replace(",", "").replace(".", "").replace(" ", "-")
    suppliers_rows.append({
        "supplier_id": sid,
        "supplier_name": name,
        "supplier_type": s_type,
        "country": country,
        "contact_email": f"sales@{safe[:25]}.com",
        "contact_phone": f"+82-{random.randint(2,64)}-{random.randint(1000,9999)}-{random.randint(1000,9999)}",
        "contract_start_date": contract_start.isoformat(),
        "quality_rating": quality_rating,
        "on_time_delivery_rate": otd,
        "status": status,
    })

with open(OUT_DIR / "suppliers.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(suppliers_rows[0].keys()))
    writer.writeheader()
    writer.writerows(suppliers_rows)


# ---------------------------------------------------------------------------
# 4. products.csv
# ---------------------------------------------------------------------------

# Korean cosmetics product catalog. 20 rows aligned with PRD-001 .. PRD-020.
PRODUCT_CATALOG = [
    ("스킨 토너 미스트",         "skincare",  18000, 150),
    ("히알루론산 세럼",           "skincare",  35000,  50),
    ("나이트 리커버리 크림",      "skincare",  42000,  60),
    ("선크림 SPF50+",            "skincare",  28000,  60),
    ("클렌징 폼",                 "skincare",  14000, 200),
    ("립 글로스 코랄",            "makeup",    16000,  10),
    ("매트 립스틱 와인",          "makeup",    22000,   8),
    ("쿠션 파운데이션 21호",      "makeup",    38000,  15),
    ("아이섀도우 팔레트",         "makeup",    52000,  35),
    ("마스카라 롱래쉬",           "makeup",    24000,  12),
    ("샴푸 데미지케어",           "haircare",  19000, 500),
    ("컨디셔너 모이스처",         "haircare",  19000, 500),
    ("헤어 에센스 오일",          "haircare",  32000, 100),
    ("헤어 트리트먼트 마스크",    "haircare",  26000, 250),
    ("드라이 샴푸",               "haircare",  17000, 150),
    ("바디 워시 라벤더",          "bodycare",  15000, 500),
    ("바디 로션 시어버터",        "bodycare",  18000, 400),
    ("핸드 크림 그린티",          "bodycare",   9000,  50),
    ("바디 스크럽 슈가",          "bodycare",  21000, 300),
    ("풋 크림 페퍼민트",          "bodycare",  12000, 100),
]
assert len(PRODUCT_CATALOG) == 20

products_rows = []
for i, (name, category, price, weight) in enumerate(PRODUCT_CATALOG, start=1):
    code = f"PRD-{i:03d}"
    shelf_life = {
        "skincare": random.choice([24, 36]),
        "makeup":   random.choice([18, 24, 36]),
        "haircare": random.choice([24, 36]),
        "bodycare": random.choice([24, 36]),
    }[category]
    primary_supplier = random.choice(SUPPLIERS)
    # 90% active, 7% discontinued, 3% pending_launch
    r = random.random()
    if r < 0.90:
        status = "active"
    elif r < 0.97:
        status = "discontinued"
    else:
        status = "pending_launch"
    launch_date = date(
        random.randint(2018, 2024),
        random.randint(1, 12),
        random.randint(1, 28),
    )
    products_rows.append({
        "product_code": code,
        "product_name": name,
        "category": category,
        "unit_price_krw": price,
        "weight_g": weight,
        "shelf_life_months": shelf_life,
        "primary_supplier_id": primary_supplier,
        "launch_date": launch_date.isoformat(),
        "status": status,
    })

with open(OUT_DIR / "products.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(products_rows[0].keys()))
    writer.writeheader()
    writer.writerows(products_rows)


# ---------------------------------------------------------------------------
# 5. inventory_movements.csv  (~20,000 rows)
# ---------------------------------------------------------------------------

INVENTORY_ROWS = 20000
WAREHOUSES = ["WH-SEOUL", "WH-INCHEON", "WH-BUSAN", "WH-DAEJEON"]
MOVEMENT_TYPES = ["IN", "OUT", "TRANSFER"]
# product → unit cost ≈ 60-75% of retail
UNIT_COST = {p["product_code"]: int(p["unit_price_krw"] * random.uniform(0.60, 0.75))
             for p in products_rows}

inventory_rows = []
# Pre-build a pool of valid order_ids for OUT movements to reference
completed_order_ids = [o["order_id"] for o in production_orders if o["status"] == "completed"]

for movement_id in range(1, INVENTORY_ROWS + 1):
    product = random.choice(PRODUCTS)
    movement_dt = random_date()
    # 45% IN, 45% OUT, 10% TRANSFER
    r = random.random()
    if r < 0.45:
        m_type = "IN"
    elif r < 0.90:
        m_type = "OUT"
    else:
        m_type = "TRANSFER"

    # Quantity: most movements small-to-medium, occasional bulk
    if random.random() < 0.85:
        qty = random.randint(10, 500)
    else:
        qty = random.randint(500, 5000)
    if m_type == "OUT":
        qty = -qty  # signed: outflow is negative

    warehouse = random.choice(WAREHOUSES)
    unit_cost = UNIT_COST[product]

    # Reference order_id only on OUT movements with ~70% probability
    if m_type == "OUT" and completed_order_ids and random.random() < 0.7:
        ref_order = random.choice(completed_order_ids)
    else:
        ref_order = ""

    # Dirty data injection
    movement_date_str = movement_dt.isoformat()
    if random.random() < 0.015:                    # ~1.5% null movement_date
        movement_date_str = ""
    elif random.random() < 0.0005:                 # ~10 future-date rows
        future = date(2026, random.randint(1, 12), random.randint(1, 28))
        movement_date_str = future.isoformat()

    qty_str = str(qty)
    if random.random() < 0.003:                    # ~60 extreme outliers
        qty_str = str(qty * random.choice([100, -100]))

    inventory_rows.append({
        "movement_id": movement_id,
        "movement_date": movement_date_str,
        "product_code": product,
        "warehouse_code": warehouse,
        "movement_type": m_type,
        "quantity": qty_str,
        "unit_cost_krw": unit_cost,
        "reference_order_id": ref_order,
    })

with open(OUT_DIR / "inventory_movements.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(inventory_rows[0].keys()))
    writer.writeheader()
    writer.writerows(inventory_rows)


print(f"production_orders.csv:     {len(production_orders):,} rows")
print(f"quality_inspections.csv:   {len(inspections):,} rows")
print(f"suppliers.csv:             {len(suppliers_rows):,} rows")
print(f"products.csv:              {len(products_rows):,} rows")
print(f"inventory_movements.csv:   {len(inventory_rows):,} rows")
