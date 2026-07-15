# Dashboard Definitions — A Complete, Real, 4-Sheet Reference

> **This is a REAL deployed dashboard definition, not a fabricated example.**
> It is the verbatim `describe-dashboard-definition` export of a manufacturing
> dashboard (`hansung-mfg-dashboard`, account `730335655603`, region
> `us-east-1`) that **passed `--validation-strategy STRICT`** and **renders
> correctly** in Amazon Quick (Quick Sight). Every visual type, field-well
> shape, format block, sort/limit, conditional-format expression, reference
> line, and grid layout below is one the service accepted and drew.
>
> Read this when you are **actually building a dashboard** and want a known-good
> shape to copy from rather than guessing the schema inline. It is large by
> design — that is fine for a reference file (loaded only when building a
> dashboard). The thin-core principle does **not** apply here.
>
> Pair it with **`dashboard-patterns.md`**: that file is the *why* (gotchas,
> the beautify checklist, the extended visual-schema catalog, KPI numerical
> accuracy); this file is a *worked, deployed answer key*. Where the two meet,
> this file cites the relevant `dashboard-patterns.md` section.

---

## What this dashboard is

A manufacturing operations dashboard ("Hansung Manufacturing Integrated Dashboard") with **4 topic
tabs (sheets)** over a single dashboard — the recommended structure
(`dashboard-patterns.md` §0): one URL, shared per-sheet date filters, one thing
to manage.

| # | Sheet (`SheetId`) | Name | Visuals | Theme |
|---|---|---|---|---|
| 1 | `eff-sheet`   | Production efficiency | 8  | Gauge + 3 KPI + bars + line + heatmap |
| 2 | `qual-sheet`  | Quality analysis      | 8  | 4 KPI (one w/ sparkline) + pie + TOP-10 bar + line + scatter |
| 3 | `cost-sheet`  | Cost analysis         | 7  | 3 KPI + combo + 2 TOP-10 bars + table |
| 4 | `deliv-sheet` | Delivery status       | 10 | 5 KPI + bars + line + table |

**11 datasets**, all from the same account/region. Note the deliberate split
between **single-row KPI datasets** (`eff-kpi`, `qual-kpi`, `cost-kpi`,
`deliv-kpi`) that back the KPI cards and **grain-level marts** (`line-daily`,
`quality-daily`, `defect-cause`, `material-cost`, `cost-comparison`, `shift`,
`delivery`) that back the trend/ranking/detail visuals. This split is the
single most important pattern in the file — see **Key patterns → #1**.

`CalculatedFields`, `ParameterDeclarations` are empty; there are **3
`FilterGroups`** (one per-sheet date filter). `ThemeArn` points at a custom
theme (`hansung-theme`).

---

## Key patterns to learn from this file

Each pattern below links to where it appears in the JSON.

1. **KPI single-row dataset + `MIN` aggregation.** Every KPI card pulls from a
   dedicated `*-kpi` dataset that has exactly **one pre-aggregated row**, and
   uses `SimpleNumericalAggregation: MIN`. Because the dataset is one row,
   `MIN`/`MAX`/`SUM`/`AVG` all return the same (correct) value — grain can't
   duplicate it. This is the fix for the "3,527 vs 426" 8.3× overcount
   (`dashboard-patterns.md` §8/§10). See `kpi-5`, `kpi-47`, `kpi-68`.

2. **Gauge with a *real* target.** `gauge-3` puts the actual KPI in `Values`
   and the **target column** (`target_utilization_pct`) in `TargetValues`,
   with `ArcAngle: 270` (one of the only allowed angles — 180/270/300/330/360)
   and `ComparisonMethod: DIFFERENCE`. The target is a real column, never a
   fabricated number (`dashboard-patterns.md` §3 extended catalog, §0 Q10).

3. **KPI sparkline + trend arrow.** `kpi-25` adds a `Sparkline` (LINE) and
   `TrendArrows`, driven by a `TrendGroups` **date field from the SAME
   grain-level dataset** (`quality-daily`), *not* from a single-row KPI dataset
   (a sparkline needs the rows). This is the exception to pattern #1.

4. **TOP-N ranking.** `bar-37`, `bar-58`, `bar-61` show the canonical TOP-10:
   `SortConfiguration` → `CategorySort` DESC **plus** `CategoryItemsLimit`
   `{ItemsLimit: 10, OtherCategories: EXCLUDE}`, horizontal orientation, data
   labels on (`dashboard-patterns.md` §11).

5. **Conditional formatting via an aggregation expression.** `kpi-9`,
   `kpi-68`, `kpi-72` color the primary value with expressions like
   `MIN({defect_rate_pct}) > 2.5` — an **aggregation**, never a raw FieldId
   (`dashboard-patterns.md` §11).

6. **`CategoricalMeasureField` for COUNT over a STRING key.** The delivery
   sheet counts orders by counting `order_key` (a string) with
   `AggregationFunction: "COUNT"` and the **`NumericFormatConfiguration`** shape
   (different from `NumericalMeasureField`!). See `bar-79`, `line-83`
   (`dashboard-patterns.md` §3).

7. **Reference line for a target.** `line-15` draws a dashed red reference line
   at `85.0` ("Target 85%") on the utilization trend.

8. **Currency formatting.** Cost visuals use `Prefix: "₩"` +
   `NumberScale: "AUTO"` + thousands separator (`dashboard-patterns.md` §11).

9. **Combo chart** (`combo-55`): standard vs actual cost — `BarValues` +
   `LineValues` sharing one category.

10. **Per-sheet date `TimeRangeFilter`** scoped to `ALL_VISUALS` on that sheet
    only (`FilterGroups` at the end).

---

## How to read the rest of this file

The complete definition follows, **reorganized for annotation**: the top-level
envelope first (with sheets/visuals elided), then each sheet's visuals as
individually valid, copy-pasteable JSON objects, then each sheet's grid layout,
then the tail (`CalculatedFields`, `FilterGroups`, `DashboardPublishOptions`).
Concatenated, the blocks reconstitute the full export. Re-indented to 2 spaces;
otherwise byte-faithful to the deployed definition.

After the answer-key export (Parts A–C) and the adapt-to-your-domain guide (Part
D), two improvement parts: **Part E** turns each "known improvement" into a
concrete, copy-paste JSON patch; **Part F** adds research-driven, best-in-class
manufacturing enhancements (defect-cause Pareto, Myriad man/eok units, color
discipline, OEE framing). Parts E–F are patches *on top of a copy* — Parts A–C
stay byte-faithful.

> **Tested.** Every JSON block in this file (53 of them — the export plus the
> Part E/F patches) parses, uses only verified Quick Sight definition-schema
> properties, has the correct `FormatConfiguration` nesting, has `FieldId`s
> consistent between field wells and `SortConfiguration`, and (for the four grid
> layouts) fits the 36-column grid with no overlaps. The export itself
> additionally passed live `--validation-strategy STRICT` and renders; the
> Part E/F patches are schema-checked against the API Reference but should still
> get a §4 STRICT probe before deploy (no credentials were used to author them).

---

# Part A — Top-level envelope

The `describe-dashboard-definition` response wraps the `Definition`. When you
feed a definition to `create-dashboard --definition file://...`, you pass the
**`Definition` object** (plus `DashboardPublishOptions`), *not* this outer
envelope (`Status`/`DashboardId`/`RequestId` are response metadata).

```json
{
  "Status": 200,
  "DashboardId": "hansung-mfg-dashboard",
  "Name": "Hansung Manufacturing Integrated Dashboard",
  "ResourceStatus": "CREATION_SUCCESSFUL",
  "ThemeArn": "arn:aws:quicksight:us-east-1:730335655603:theme/hansung-theme",
  "Definition": {
    "DataSetIdentifierDeclarations": "... see below ...",
    "Sheets": "... 4 sheets, see Part B ...",
    "CalculatedFields": [],
    "ParameterDeclarations": [],
    "FilterGroups": "... 3 filter groups, see Part C ..."
  },
  "RequestId": "09bf3f94-d945-49ce-b5c9-31296c25b283",
  "DashboardPublishOptions": "... see Part C ..."
}
```

## `DataSetIdentifierDeclarations` — the 11 datasets

Each visual references a dataset by its **`Identifier`** (the short string),
never by ARN. The ARN→identifier mapping lives here once. Note the four
single-row KPI datasets (`cost-kpi`, `eff-kpi`, `deliv-kpi`, `qual-kpi`) vs the
grain-level marts.

```json
[
  {
    "Identifier": "delivery",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-delivery"
  },
  {
    "Identifier": "cost-comparison",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-cost-comparison"
  },
  {
    "Identifier": "quality-daily",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-quality-daily"
  },
  {
    "Identifier": "shift",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-shift"
  },
  {
    "Identifier": "defect-cause",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-defect-cause"
  },
  {
    "Identifier": "material-cost",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-material-cost"
  },
  {
    "Identifier": "cost-kpi",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-cost-kpi"
  },
  {
    "Identifier": "eff-kpi",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-eff-kpi"
  },
  {
    "Identifier": "deliv-kpi",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-deliv-kpi"
  },
  {
    "Identifier": "line-daily",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-line-daily"
  },
  {
    "Identifier": "qual-kpi",
    "DataSetArn": "arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-qual-kpi"
  }
]
```


---

# Part B — Sheets & visuals

## Sheet 1 — `eff-sheet` Production Efficiency

8 visuals: a hero gauge (utilization vs 85% target), three single-row KPI
cards, a per-line bar, a daily trend line with a target reference line, a
shift-defect bar, and a line×date heatmap. Datasets: `eff-kpi` (single-row) for
the cards/gauge, `line-daily` (grain) for the bar/line/heatmap, `shift` for the
shift bar.

### `gauge-3` — GaugeChartVisual — Utilization vs Target (85%)

**Pattern #2 — gauge with a real target.** `Values` = actual `avg_utilization_pct`; `TargetValues` = `target_utilization_pct` (a real column). `ArcAngle: 270.0` is one of the only accepted angles. `ComparisonMethod: DIFFERENCE` shows the gap to target. KPI dataset → `MIN` aggregation (single row). Both wells format with a `%` suffix.

```json
{
  "GaugeChartVisual": {
    "VisualId": "gauge-3",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Utilization vs Target (85%)"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-1",
              "Column": {
                "DataSetIdentifier": "eff-kpi",
                "ColumnName": "avg_utilization_pct"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "%",
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 1
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-2",
              "Column": {
                "DataSetIdentifier": "eff-kpi",
                "ColumnName": "target_utilization_pct"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "%",
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 0
                    }
                  }
                }
              }
            }
          }
        ]
      },
      "GaugeChartOptions": {
        "Comparison": {
          "ComparisonMethod": "DIFFERENCE"
        },
        "Arc": {
          "ArcAngle": 270.0
        }
      },
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": []
  }
}
```


### `kpi-5` — KPIVisual — Average Cycle Time

**Pattern #1 — single-row KPI card.** `eff-kpi.avg_cycle_time_sec` with `MIN` aggregation, `sec` (seconds) suffix, 1 decimal, thousands separator. `FontSize.Relative: EXTRA_LARGE` makes it a hero number. Empty `TargetValues`/`TrendGroups` — a plain KPI needs neither.

```json
{
  "KPIVisual": {
    "VisualId": "kpi-5",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Average Cycle Time"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-4",
              "Column": {
                "DataSetIdentifier": "eff-kpi",
                "ColumnName": "avg_cycle_time_sec"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "sec",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 1
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-7` — KPIVisual — Total Production

Single-row KPI: total production qty, ` EA` suffix, 0 decimals.

```json
{
  "KPIVisual": {
    "VisualId": "kpi-7",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Total Production"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-6",
              "Column": {
                "DataSetIdentifier": "eff-kpi",
                "ColumnName": "total_production_qty"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": " EA",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 0
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-9` — KPIVisual — Defect Rate (weighted)

**Pattern #5 — conditional formatting by aggregation expression.** Defect rate KPI colored red when `MIN({defect_rate_pct}) > 2.5`, green when `<= 2.0`. The expression uses an **aggregation** (`MIN`), never a raw FieldId — that is the rule (`dashboard-patterns.md` §11).

```json
{
  "KPIVisual": {
    "VisualId": "kpi-9",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Defect Rate (weighted)"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-8",
              "Column": {
                "DataSetIdentifier": "eff-kpi",
                "ColumnName": "defect_rate_pct"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "%",
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 2
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "ConditionalFormatting": {
      "ConditionalFormattingOptions": [
        {
          "PrimaryValue": {
            "TextColor": {
              "Solid": {
                "Expression": "MIN({defect_rate_pct}) > 2.5",
                "Color": "#D1242F"
              }
            }
          }
        },
        {
          "PrimaryValue": {
            "TextColor": {
              "Solid": {
                "Expression": "MIN({defect_rate_pct}) <= 2.0",
                "Color": "#2EA043"
              }
            }
          }
        }
      ]
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `bar-12` — BarChartVisual — Utilization by Line

Bar chart from the **grain-level** `line-daily` mart (it needs the per-line rows). `Category` = `line_code`, `Values` = `AVERAGE` of `utilization_pct`. `CategorySort` DESC on the measure. Vertical, data labels on.

```json
{
  "BarChartVisual": {
    "VisualId": "bar-12",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Utilization by Line"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "BarChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-10",
                "Column": {
                  "DataSetIdentifier": "line-daily",
                  "ColumnName": "line_code"
                }
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-11",
                "Column": {
                  "DataSetIdentifier": "line-daily",
                  "ColumnName": "utilization_pct"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "AVERAGE"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Suffix": "%",
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 1
                      }
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          {
            "FieldSort": {
              "FieldId": "m-11",
              "Direction": "DESC"
            }
          }
        ]
      },
      "Orientation": "VERTICAL",
      "BarsArrangement": "CLUSTERED",
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `line-15` — LineChartVisual — Daily Overall Utilization Trend

**Pattern #7 — reference line target.** Daily utilization trend (`DateDimensionField`, `DateGranularity: DAY`, with a `DateTimeHierarchy`). A `ReferenceLines` entry draws a dashed red static line at `85.0` labeled “Target 85%”. Note the matching `ColumnHierarchies` entry — date fields with a `HierarchyId` must declare the hierarchy.

```json
{
  "LineChartVisual": {
    "VisualId": "line-15",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Daily Overall Utilization Trend"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "LineChartAggregatedFieldWells": {
          "Category": [
            {
              "DateDimensionField": {
                "FieldId": "dt-13",
                "Column": {
                  "DataSetIdentifier": "line-daily",
                  "ColumnName": "production_date"
                },
                "DateGranularity": "DAY",
                "HierarchyId": "dt-13"
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-14",
                "Column": {
                  "DataSetIdentifier": "line-daily",
                  "ColumnName": "utilization_pct"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "AVERAGE"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Suffix": "%",
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 1
                      }
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {},
      "Type": "LINE",
      "ReferenceLines": [
        {
          "Status": "ENABLED",
          "DataConfiguration": {
            "StaticConfiguration": {
              "Value": 85.0
            },
            "AxisBinding": "PRIMARY_YAXIS",
            "SeriesType": "LINE"
          },
          "StyleConfiguration": {
            "Pattern": "DASHED",
            "Color": "#D1242F"
          },
          "LabelConfiguration": {
            "CustomLabelConfiguration": {
              "CustomLabel": "Target 85%"
            },
            "FontConfiguration": {
              "FontSize": {
                "Relative": "MEDIUM"
              }
            },
            "FontColor": "#D1242F",
            "HorizontalPosition": "RIGHT",
            "VerticalPosition": "ABOVE"
          }
        }
      ]
    },
    "Actions": [],
    "ColumnHierarchies": [
      {
        "DateTimeHierarchy": {
          "HierarchyId": "dt-13"
        }
      }
    ]
  }
}
```


### `bar-22` — BarChartVisual — Defect Rate by Work Shift [insight]

Defect rate by work shift, from the `shift` mart. `[insight]` in the title marks an insight-bearing visual (a project convention, not a schema feature).

```json
{
  "BarChartVisual": {
    "VisualId": "bar-22",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Defect Rate by Work Shift [insight]"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "BarChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-20",
                "Column": {
                  "DataSetIdentifier": "shift",
                  "ColumnName": "shift"
                }
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-21",
                "Column": {
                  "DataSetIdentifier": "shift",
                  "ColumnName": "defect_rate_pct"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "AVERAGE"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Suffix": "%",
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 2
                      }
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          {
            "FieldSort": {
              "FieldId": "m-21",
              "Direction": "DESC"
            }
          }
        ]
      },
      "Orientation": "VERTICAL",
      "BarsArrangement": "CLUSTERED",
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `heat-19` — HeatMapVisual — Line × Date Utilization Heatmap

**HeatMap** — line × date utilization. `HeatMapAggregatedFieldWells` uses `Rows` (line_code), `Columns` (date), `Values` (AVERAGE utilization). Like the line chart it declares a `DateTimeHierarchy`.

```json
{
  "HeatMapVisual": {
    "VisualId": "heat-19",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Line × Date Utilization Heatmap"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "HeatMapAggregatedFieldWells": {
          "Rows": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-16",
                "Column": {
                  "DataSetIdentifier": "line-daily",
                  "ColumnName": "line_code"
                }
              }
            }
          ],
          "Columns": [
            {
              "DateDimensionField": {
                "FieldId": "dt-17",
                "Column": {
                  "DataSetIdentifier": "line-daily",
                  "ColumnName": "production_date"
                },
                "DateGranularity": "DAY",
                "HierarchyId": "dt-17"
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-18",
                "Column": {
                  "DataSetIdentifier": "line-daily",
                  "ColumnName": "utilization_pct"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "AVERAGE"
                }
              }
            }
          ]
        }
      },
      "SortConfiguration": {}
    },
    "ColumnHierarchies": [
      {
        "DateTimeHierarchy": {
          "HierarchyId": "dt-17"
        }
      }
    ],
    "Actions": []
  }
}
```


### `eff-sheet` — grid layout (`Layouts`)

The 36-column `GridLayout`. Each element ties a `VisualId` to a `ColumnIndex`/`ColumnSpan`/`RowIndex`/`RowSpan`. Verify `ColumnIndex + ColumnSpan <= 36` and no overlap (`dashboard-patterns.md` §9).

```json
[
  {
    "Configuration": {
      "GridLayout": {
        "Elements": [
          {
            "ElementId": "gauge-3",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 12,
            "RowIndex": 0,
            "RowSpan": 8
          },
          {
            "ElementId": "kpi-5",
            "ElementType": "VISUAL",
            "ColumnIndex": 12,
            "ColumnSpan": 8,
            "RowIndex": 0,
            "RowSpan": 4
          },
          {
            "ElementId": "kpi-7",
            "ElementType": "VISUAL",
            "ColumnIndex": 20,
            "ColumnSpan": 8,
            "RowIndex": 0,
            "RowSpan": 4
          },
          {
            "ElementId": "kpi-9",
            "ElementType": "VISUAL",
            "ColumnIndex": 28,
            "ColumnSpan": 8,
            "RowIndex": 0,
            "RowSpan": 4
          },
          {
            "ElementId": "bar-12",
            "ElementType": "VISUAL",
            "ColumnIndex": 12,
            "ColumnSpan": 24,
            "RowIndex": 4,
            "RowSpan": 4
          },
          {
            "ElementId": "line-15",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 18,
            "RowIndex": 8,
            "RowSpan": 10
          },
          {
            "ElementId": "bar-22",
            "ElementType": "VISUAL",
            "ColumnIndex": 18,
            "ColumnSpan": 18,
            "RowIndex": 8,
            "RowSpan": 10
          },
          {
            "ElementId": "heat-19",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 36,
            "RowIndex": 18,
            "RowSpan": 10
          }
        ]
      }
    }
  }
]
```


`ContentType`: `INTERACTIVE`


---

## Sheet 2 — `qual-sheet` Quality Analysis

8 visuals. The standout is `kpi-25`, the **only KPI card backed by a
grain-level dataset** — it needs daily rows for its sparkline. The rest follow
the single-row pattern. Includes a donut, a TOP-10 bar, a multi-series line,
and a scatter correlating MES floor defects with QMEL notifications.

### `kpi-25` — KPIVisual — Defect Rate Trend

**Pattern #3 — KPI with sparkline + trend arrow.** This card pulls from the **grain-level** `quality-daily` mart (NOT a single-row KPI dataset) precisely because a sparkline needs the daily rows. `TrendGroups` carries a `DateDimensionField` (`production_date`, DAY) from that same dataset, and `KPIOptions` enables `TrendArrows` + a LINE `Sparkline`. Aggregation is `AVERAGE` (averaging the daily rate), not `MIN`.

```json
{
  "KPIVisual": {
    "VisualId": "kpi-25",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Defect Rate Trend"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-23",
              "Column": {
                "DataSetIdentifier": "quality-daily",
                "ColumnName": "day_defect_rate_pct"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "AVERAGE"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "%",
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 2
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": [
          {
            "DateDimensionField": {
              "FieldId": "dt-24",
              "Column": {
                "DataSetIdentifier": "quality-daily",
                "ColumnName": "production_date"
              },
              "DateGranularity": "DAY",
              "HierarchyId": "dt-24"
            }
          }
        ]
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "TrendArrows": {
          "Visibility": "VISIBLE"
        },
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        },
        "Sparkline": {
          "Visibility": "VISIBLE",
          "Type": "LINE",
          "TooltipVisibility": "HIDDEN"
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": [
      {
        "DateTimeHierarchy": {
          "HierarchyId": "dt-24"
        }
      }
    ]
  }
}
```


### `kpi-27` — KPIVisual — Total Defect Quantity

Single-row KPI from `qual-kpi`: total defect qty, ` EA`.

```json
{
  "KPIVisual": {
    "VisualId": "kpi-27",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Total Defect Quantity"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-26",
              "Column": {
                "DataSetIdentifier": "qual-kpi",
                "ColumnName": "total_defect_qty"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": " EA",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 0
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-29` — KPIVisual — Defective Material Count

Single-row KPI: defect material **count**. In the source mart this is pre-computed as `COUNT(DISTINCT material_key)` so the card can safely read it with `MIN` — the distinct-count fix lives in the pipeline, not the field well (`dashboard-patterns.md` §8 root-cause #4).

```json
{
  "KPIVisual": {
    "VisualId": "kpi-29",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Defective Material Count"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-28",
              "Column": {
                "DataSetIdentifier": "qual-kpi",
                "ColumnName": "defect_material_count"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "count",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 0
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-31` — KPIVisual — QMEL Notifications

Single-row KPI: QMEL notification count (`count`). This is the KPI that famously showed 3,527 (8.3× overcount) when fed a multi-grain mart; here it reads the single-row `qual-kpi` (§8/§10).

```json
{
  "KPIVisual": {
    "VisualId": "kpi-31",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "QMEL Notifications"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-30",
              "Column": {
                "DataSetIdentifier": "qual-kpi",
                "ColumnName": "qmel_notification_count"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "cases",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 0
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `pie-34` — PieChartVisual — Quantity by Defect Type

**Donut/Pie** — defect qty by defect type. `DonutOptions.ArcOptions.ArcThickness: MEDIUM` makes it a donut. Category + measure labels both visible.

```json
{
  "PieChartVisual": {
    "VisualId": "pie-34",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Quantity by Defect Type"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "PieChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-32",
                "Column": {
                  "DataSetIdentifier": "defect-cause",
                  "ColumnName": "defect_name"
                }
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-33",
                "Column": {
                  "DataSetIdentifier": "defect-cause",
                  "ColumnName": "mes_defect_qty"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ]
        }
      },
      "SortConfiguration": {},
      "DonutOptions": {
        "ArcOptions": {
          "ArcThickness": "MEDIUM"
        }
      },
      "DataLabels": {
        "Visibility": "VISIBLE",
        "CategoryLabelVisibility": "VISIBLE",
        "MeasureLabelVisibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `bar-37` — BarChartVisual — Top 10 Defect Quantity by Material

**Pattern #4 — TOP-10.** Defect qty by material: `CategorySort` DESC + `CategoryItemsLimit {ItemsLimit: 10, OtherCategories: EXCLUDE}`, `Orientation: HORIZONTAL`. The empty `ColorItemsLimit`/`SmallMultiplesLimitConfiguration` blocks are emitted by the export but harmless.

```json
{
  "BarChartVisual": {
    "VisualId": "bar-37",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Top 10 Defect Quantity by Material"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "BarChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-35",
                "Column": {
                  "DataSetIdentifier": "defect-cause",
                  "ColumnName": "material_name"
                }
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-36",
                "Column": {
                  "DataSetIdentifier": "defect-cause",
                  "ColumnName": "mes_defect_qty"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          {
            "FieldSort": {
              "FieldId": "m-36",
              "Direction": "DESC"
            }
          }
        ],
        "CategoryItemsLimit": {
          "ItemsLimit": 10,
          "OtherCategories": "EXCLUDE"
        },
        "ColorItemsLimit": {
          "OtherCategories": "EXCLUDE"
        },
        "SmallMultiplesLimitConfiguration": {
          "OtherCategories": "EXCLUDE"
        }
      },
      "Orientation": "HORIZONTAL",
      "BarsArrangement": "CLUSTERED",
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `line-41` — LineChartVisual — Daily Trend by Defect Type

Multi-series line: daily defect qty by `defect_name` (the series split lives in `Colors`). `SUM` of `defect_qty` over the `quality-daily` mart.

```json
{
  "LineChartVisual": {
    "VisualId": "line-41",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Daily Trend by Defect Type"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "LineChartAggregatedFieldWells": {
          "Category": [
            {
              "DateDimensionField": {
                "FieldId": "dt-38",
                "Column": {
                  "DataSetIdentifier": "quality-daily",
                  "ColumnName": "production_date"
                },
                "DateGranularity": "DAY",
                "HierarchyId": "dt-38"
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-39",
                "Column": {
                  "DataSetIdentifier": "quality-daily",
                  "ColumnName": "defect_qty"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ],
          "Colors": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-40",
                "Column": {
                  "DataSetIdentifier": "quality-daily",
                  "ColumnName": "defect_name"
                }
              }
            }
          ]
        }
      },
      "SortConfiguration": {},
      "Type": "LINE"
    },
    "Actions": [],
    "ColumnHierarchies": [
      {
        "DateTimeHierarchy": {
          "HierarchyId": "dt-38"
        }
      }
    ]
  }
}
```


### `sc-45` — ScatterPlotVisual — MES Field Defects vs QMEL Notifications [insight]

**ScatterPlot** — MES floor defects (X) vs QMEL notifications (Y) per material. `ScatterPlotCategoricallyAggregatedFieldWells` with X/Y measures + a `Category` dimension. Note X uses `SUM`, Y uses `MAX`.

```json
{
  "ScatterPlotVisual": {
    "VisualId": "sc-45",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "MES Field Defects vs QMEL Notifications [insight]"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "ScatterPlotCategoricallyAggregatedFieldWells": {
          "XAxis": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-42",
                "Column": {
                  "DataSetIdentifier": "defect-cause",
                  "ColumnName": "mes_defect_qty"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ],
          "YAxis": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-43",
                "Column": {
                  "DataSetIdentifier": "defect-cause",
                  "ColumnName": "qmel_notification_count"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "MAX"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ],
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-44",
                "Column": {
                  "DataSetIdentifier": "defect-cause",
                  "ColumnName": "material_name"
                }
              }
            }
          ],
          "Size": [],
          "Label": []
        }
      },
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `qual-sheet` — grid layout (`Layouts`)

The 36-column `GridLayout`. Each element ties a `VisualId` to a `ColumnIndex`/`ColumnSpan`/`RowIndex`/`RowSpan`. Verify `ColumnIndex + ColumnSpan <= 36` and no overlap (`dashboard-patterns.md` §9).

```json
[
  {
    "Configuration": {
      "GridLayout": {
        "Elements": [
          {
            "ElementId": "kpi-25",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 12,
            "RowIndex": 0,
            "RowSpan": 6
          },
          {
            "ElementId": "kpi-27",
            "ElementType": "VISUAL",
            "ColumnIndex": 12,
            "ColumnSpan": 8,
            "RowIndex": 0,
            "RowSpan": 6
          },
          {
            "ElementId": "kpi-29",
            "ElementType": "VISUAL",
            "ColumnIndex": 20,
            "ColumnSpan": 8,
            "RowIndex": 0,
            "RowSpan": 6
          },
          {
            "ElementId": "kpi-31",
            "ElementType": "VISUAL",
            "ColumnIndex": 28,
            "ColumnSpan": 8,
            "RowIndex": 0,
            "RowSpan": 6
          },
          {
            "ElementId": "pie-34",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 18,
            "RowIndex": 6,
            "RowSpan": 10
          },
          {
            "ElementId": "bar-37",
            "ElementType": "VISUAL",
            "ColumnIndex": 18,
            "ColumnSpan": 18,
            "RowIndex": 6,
            "RowSpan": 10
          },
          {
            "ElementId": "line-41",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 18,
            "RowIndex": 16,
            "RowSpan": 10
          },
          {
            "ElementId": "sc-45",
            "ElementType": "VISUAL",
            "ColumnIndex": 18,
            "ColumnSpan": 18,
            "RowIndex": 16,
            "RowSpan": 10
          }
        ]
      }
    }
  }
]
```


`ContentType`: `INTERACTIVE`


---

## Sheet 3 — `cost-sheet` Cost Analysis

7 visuals, all about money — every measure uses the `₩` + `NumberScale: AUTO`
currency format. Three KPI cards, a standard-vs-actual combo chart, two TOP-10
bars, and a variance comparison table.

### `kpi-47` — KPIVisual — Total Consumption Cost

**Pattern #8 — currency.** Total consumption cost: `Prefix: "₩"`, `NumberScale: "AUTO"` (renders ₩1.2B / ₩340M), thousands separator, 1 decimal. Single-row `cost-kpi`, `MIN`.

```json
{
  "KPIVisual": {
    "VisualId": "kpi-47",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Total Consumption Cost"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-46",
              "Column": {
                "DataSetIdentifier": "cost-kpi",
                "ColumnName": "total_consumption_cost_kwon"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Prefix": "₩",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 1
                    },
                    "NumberScale": "AUTO"
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-49` — KPIVisual — Total Scrap Quantity

Single-row KPI: total scrap qty (` EA`).

```json
{
  "KPIVisual": {
    "VisualId": "kpi-49",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Total Scrap Quantity"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-48",
              "Column": {
                "DataSetIdentifier": "cost-kpi",
                "ColumnName": "total_scrap_qty"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": " EA",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 0
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-51` — KPIVisual — Total Scrap Loss [insight]

Single-row KPI: total scrap **loss** in ₩ (insight metric).

```json
{
  "KPIVisual": {
    "VisualId": "kpi-51",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Total Scrap Loss [insight]"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-50",
              "Column": {
                "DataSetIdentifier": "cost-kpi",
                "ColumnName": "total_scrap_cost_kwon"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Prefix": "₩",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 1
                    },
                    "NumberScale": "AUTO"
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `combo-55` — ComboChartVisual — Standard vs Actual Cost by Product Group

**Pattern #9 — combo chart.** Standard vs actual cost by product group. `BarValues` = `sap_standard_cost_kwon`, `LineValues` = `finance_actual_cost_kwon`, sharing one `Category`. Separate `BarDataLabels` and `LineDataLabels` blocks. From the grain-level `cost-comparison` mart.

```json
{
  "ComboChartVisual": {
    "VisualId": "combo-55",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Standard vs Actual Cost by Product Group"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "ComboChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-52",
                "Column": {
                  "DataSetIdentifier": "cost-comparison",
                  "ColumnName": "finance_product_group"
                }
              }
            }
          ],
          "BarValues": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-53",
                "Column": {
                  "DataSetIdentifier": "cost-comparison",
                  "ColumnName": "sap_standard_cost_kwon"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Prefix": "₩",
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 1
                      },
                      "NumberScale": "AUTO"
                    }
                  }
                }
              }
            }
          ],
          "Colors": [],
          "LineValues": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-54",
                "Column": {
                  "DataSetIdentifier": "cost-comparison",
                  "ColumnName": "finance_actual_cost_kwon"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Prefix": "₩",
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 1
                      },
                      "NumberScale": "AUTO"
                    }
                  }
                }
              }
            }
          ]
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          {
            "FieldSort": {
              "FieldId": "m-53",
              "Direction": "DESC"
            }
          }
        ]
      },
      "BarsArrangement": "CLUSTERED",
      "BarDataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      },
      "LineDataLabels": {
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `bar-58` — BarChartVisual — Top 10 Consumption Cost by Material

TOP-10 material consumption cost (₩, AUTO scale). Same TOP-N pattern as `bar-37`.

```json
{
  "BarChartVisual": {
    "VisualId": "bar-58",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Top 10 Consumption Cost by Material"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "BarChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-56",
                "Column": {
                  "DataSetIdentifier": "material-cost",
                  "ColumnName": "material_name"
                }
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-57",
                "Column": {
                  "DataSetIdentifier": "material-cost",
                  "ColumnName": "consumption_cost_kwon"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Prefix": "₩",
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 1
                      },
                      "NumberScale": "AUTO"
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          {
            "FieldSort": {
              "FieldId": "m-57",
              "Direction": "DESC"
            }
          }
        ],
        "CategoryItemsLimit": {
          "ItemsLimit": 10,
          "OtherCategories": "EXCLUDE"
        },
        "ColorItemsLimit": {
          "OtherCategories": "EXCLUDE"
        },
        "SmallMultiplesLimitConfiguration": {
          "OtherCategories": "EXCLUDE"
        }
      },
      "Orientation": "HORIZONTAL",
      "BarsArrangement": "CLUSTERED",
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `bar-61` — BarChartVisual — Top 10 Scrap Loss by Material [insight]

TOP-10 material scrap loss (₩). Insight visual.

```json
{
  "BarChartVisual": {
    "VisualId": "bar-61",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Top 10 Scrap Loss by Material [insight]"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "BarChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-59",
                "Column": {
                  "DataSetIdentifier": "material-cost",
                  "ColumnName": "material_name"
                }
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-60",
                "Column": {
                  "DataSetIdentifier": "material-cost",
                  "ColumnName": "scrap_cost_kwon"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Prefix": "₩",
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 1
                      },
                      "NumberScale": "AUTO"
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          {
            "FieldSort": {
              "FieldId": "m-60",
              "Direction": "DESC"
            }
          }
        ],
        "CategoryItemsLimit": {
          "ItemsLimit": 10,
          "OtherCategories": "EXCLUDE"
        },
        "ColorItemsLimit": {
          "OtherCategories": "EXCLUDE"
        },
        "SmallMultiplesLimitConfiguration": {
          "OtherCategories": "EXCLUDE"
        }
      },
      "Orientation": "HORIZONTAL",
      "BarsArrangement": "CLUSTERED",
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `tbl-66` — TableVisual — Standard vs Actual Cost Comparison Table

**TableVisual** — standard vs actual vs variance%. `TableAggregatedFieldWells` with `GroupBy` (product group) + three `Values`. Mixed formatting: two ₩ columns + one `%` column (`variance_pct`, AVERAGE). Tables have no `ColumnHierarchies` key.

```json
{
  "TableVisual": {
    "VisualId": "tbl-66",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Standard vs Actual Cost Comparison Table"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "TableAggregatedFieldWells": {
          "GroupBy": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-62",
                "Column": {
                  "DataSetIdentifier": "cost-comparison",
                  "ColumnName": "finance_product_group"
                }
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-63",
                "Column": {
                  "DataSetIdentifier": "cost-comparison",
                  "ColumnName": "sap_standard_cost_kwon"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Prefix": "₩",
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 1
                      },
                      "NumberScale": "AUTO"
                    }
                  }
                }
              }
            },
            {
              "NumericalMeasureField": {
                "FieldId": "m-64",
                "Column": {
                  "DataSetIdentifier": "cost-comparison",
                  "ColumnName": "finance_actual_cost_kwon"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "SUM"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Prefix": "₩",
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 1
                      },
                      "NumberScale": "AUTO"
                    }
                  }
                }
              }
            },
            {
              "NumericalMeasureField": {
                "FieldId": "m-65",
                "Column": {
                  "DataSetIdentifier": "cost-comparison",
                  "ColumnName": "variance_pct"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "AVERAGE"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Suffix": "%",
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 1
                      }
                    }
                  }
                }
              }
            }
          ]
        }
      },
      "SortConfiguration": {}
    },
    "Actions": []
  }
}
```


### `cost-sheet` — grid layout (`Layouts`)

The 36-column `GridLayout`. Each element ties a `VisualId` to a `ColumnIndex`/`ColumnSpan`/`RowIndex`/`RowSpan`. Verify `ColumnIndex + ColumnSpan <= 36` and no overlap (`dashboard-patterns.md` §9).

```json
[
  {
    "Configuration": {
      "GridLayout": {
        "Elements": [
          {
            "ElementId": "kpi-47",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 12,
            "RowIndex": 0,
            "RowSpan": 5
          },
          {
            "ElementId": "kpi-49",
            "ElementType": "VISUAL",
            "ColumnIndex": 12,
            "ColumnSpan": 12,
            "RowIndex": 0,
            "RowSpan": 5
          },
          {
            "ElementId": "kpi-51",
            "ElementType": "VISUAL",
            "ColumnIndex": 24,
            "ColumnSpan": 12,
            "RowIndex": 0,
            "RowSpan": 5
          },
          {
            "ElementId": "combo-55",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 36,
            "RowIndex": 5,
            "RowSpan": 10
          },
          {
            "ElementId": "bar-58",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 18,
            "RowIndex": 15,
            "RowSpan": 9
          },
          {
            "ElementId": "bar-61",
            "ElementType": "VISUAL",
            "ColumnIndex": 18,
            "ColumnSpan": 18,
            "RowIndex": 15,
            "RowSpan": 9
          },
          {
            "ElementId": "tbl-66",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 36,
            "RowIndex": 24,
            "RowSpan": 7
          }
        ]
      }
    }
  }
]
```


`ContentType`: `INTERACTIVE`


---

## Sheet 4 — `deliv-sheet` Delivery Status

10 visuals (the densest sheet). Five KPI cards (three with conditional
formatting), then four charts that all **count orders via
`CategoricalMeasureField`** (because `order_key` is a string), and a detail
table of the worst-delayed orders.

### `kpi-68` — KPIVisual — On-Time Delivery Rate

**Conditional formatting (good/bad).** On-time rate: green when `MIN({on_time_rate_pct}) >= 80`, red when `< 50`. Single-row `deliv-kpi`.

```json
{
  "KPIVisual": {
    "VisualId": "kpi-68",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "On-Time Delivery Rate"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-67",
              "Column": {
                "DataSetIdentifier": "deliv-kpi",
                "ColumnName": "on_time_rate_pct"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "%",
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 1
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "ConditionalFormatting": {
      "ConditionalFormattingOptions": [
        {
          "PrimaryValue": {
            "TextColor": {
              "Solid": {
                "Expression": "MIN({on_time_rate_pct}) >= 80",
                "Color": "#2EA043"
              }
            }
          }
        },
        {
          "PrimaryValue": {
            "TextColor": {
              "Solid": {
                "Expression": "MIN({on_time_rate_pct}) < 50",
                "Color": "#D1242F"
              }
            }
          }
        }
      ]
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-70` — KPIVisual — Total Production Orders

Single-row KPI: total production orders (`cases`).

```json
{
  "KPIVisual": {
    "VisualId": "kpi-70",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Total Production Orders"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-69",
              "Column": {
                "DataSetIdentifier": "deliv-kpi",
                "ColumnName": "total_orders"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "cases",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 0
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-72` — KPIVisual — Late Order Count

Single-row KPI: late orders (`cases`), red when `MIN({late_orders}) > 400`. The correct population filter (`WHERE is_on_time = false`) is baked into the mart, not the field well (§8 root-cause #3).

```json
{
  "KPIVisual": {
    "VisualId": "kpi-72",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Late Order Count"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-71",
              "Column": {
                "DataSetIdentifier": "deliv-kpi",
                "ColumnName": "late_orders"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "cases",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 0
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "ConditionalFormatting": {
      "ConditionalFormattingOptions": [
        {
          "PrimaryValue": {
            "TextColor": {
              "Solid": {
                "Expression": "MIN({late_orders}) > 400",
                "Color": "#D1242F"
              }
            }
          }
        }
      ]
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-74` — KPIVisual — Average Delay Days

Single-row KPI: avg delay **days, late only**. The mart excludes early completions (`AVG(CASE WHEN delay_days > 0 …)`) so negatives don't dilute it (§8 root-cause #5).

```json
{
  "KPIVisual": {
    "VisualId": "kpi-74",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Average Delay Days"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-73",
              "Column": {
                "DataSetIdentifier": "deliv-kpi",
                "ColumnName": "avg_delay_days_late"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "days",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 1
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `kpi-76` — KPIVisual — Missing Planned Date

Single-row KPI: orders missing a planned date (`cases`).

```json
{
  "KPIVisual": {
    "VisualId": "kpi-76",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Missing Planned Date"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "Values": [
          {
            "NumericalMeasureField": {
              "FieldId": "m-75",
              "Column": {
                "DataSetIdentifier": "deliv-kpi",
                "ColumnName": "no_plan_orders"
              },
              "AggregationFunction": {
                "SimpleNumericalAggregation": "MIN"
              },
              "FormatConfiguration": {
                "FormatConfiguration": {
                  "NumberDisplayFormatConfiguration": {
                    "Suffix": "cases",
                    "SeparatorConfiguration": {
                      "ThousandsSeparator": {
                        "Symbol": "COMMA",
                        "Visibility": "VISIBLE"
                      }
                    },
                    "DecimalPlacesConfiguration": {
                      "DecimalPlaces": 0
                    }
                  }
                }
              }
            }
          }
        ],
        "TargetValues": [],
        "TrendGroups": []
      },
      "SortConfiguration": {},
      "KPIOptions": {
        "PrimaryValueFontConfiguration": {
          "FontSize": {
            "Relative": "EXTRA_LARGE"
          }
        }
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `bar-79` — BarChartVisual — Count by Delivery Status

**Pattern #6 — `CategoricalMeasureField` COUNT.** Order count by `on_time_label`. `order_key` is a STRING, so it uses `CategoricalMeasureField` with `AggregationFunction: "COUNT"` and the `NumericFormatConfiguration` shape — NOT `NumericalMeasureField` (which rejects STRING columns). This exact mismatch is a top gotcha (`dashboard-patterns.md` §3).

```json
{
  "BarChartVisual": {
    "VisualId": "bar-79",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Count by Delivery Status"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "BarChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-77",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "on_time_label"
                }
              }
            }
          ],
          "Values": [
            {
              "CategoricalMeasureField": {
                "FieldId": "cm-78",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "order_key"
                },
                "AggregationFunction": "COUNT",
                "FormatConfiguration": {
                  "NumericFormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          {
            "FieldSort": {
              "FieldId": "cm-78",
              "Direction": "DESC"
            }
          }
        ]
      },
      "Orientation": "VERTICAL",
      "BarsArrangement": "CLUSTERED",
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `line-83` — LineChartVisual — Delay Trend by Planned Due Date

Delay trend by planned finish date, series split by `on_time_label`. Again a `CategoricalMeasureField` COUNT of `order_key`.

```json
{
  "LineChartVisual": {
    "VisualId": "line-83",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Delay Trend by Planned Due Date"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "LineChartAggregatedFieldWells": {
          "Category": [
            {
              "DateDimensionField": {
                "FieldId": "dt-80",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "planned_finish"
                },
                "DateGranularity": "DAY",
                "HierarchyId": "dt-80"
              }
            }
          ],
          "Values": [
            {
              "CategoricalMeasureField": {
                "FieldId": "cm-81",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "order_key"
                },
                "AggregationFunction": "COUNT",
                "FormatConfiguration": {
                  "NumericFormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ],
          "Colors": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-82",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "on_time_label"
                }
              }
            }
          ]
        }
      },
      "SortConfiguration": {},
      "Type": "LINE"
    },
    "Actions": [],
    "ColumnHierarchies": [
      {
        "DateTimeHierarchy": {
          "HierarchyId": "dt-80"
        }
      }
    ]
  }
}
```


### `bar-86` — BarChartVisual — Delivery Status by Material Group

Order count by material group (`CategoricalMeasureField` COUNT).

```json
{
  "BarChartVisual": {
    "VisualId": "bar-86",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Delivery Status by Material Group"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "BarChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-84",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "material_group"
                }
              }
            }
          ],
          "Values": [
            {
              "CategoricalMeasureField": {
                "FieldId": "cm-85",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "order_key"
                },
                "AggregationFunction": "COUNT",
                "FormatConfiguration": {
                  "NumericFormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          {
            "FieldSort": {
              "FieldId": "cm-85",
              "Direction": "DESC"
            }
          }
        ]
      },
      "Orientation": "VERTICAL",
      "BarsArrangement": "CLUSTERED",
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `bar-89` — BarChartVisual — Delivery Status by Plant

Order count by plant (`CategoricalMeasureField` COUNT).

```json
{
  "BarChartVisual": {
    "VisualId": "bar-89",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Delivery Status by Plant"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "BarChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-87",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "plant_name"
                }
              }
            }
          ],
          "Values": [
            {
              "CategoricalMeasureField": {
                "FieldId": "cm-88",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "order_key"
                },
                "AggregationFunction": "COUNT",
                "FormatConfiguration": {
                  "NumericFormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          {
            "FieldSort": {
              "FieldId": "cm-88",
              "Direction": "DESC"
            }
          }
        ]
      },
      "Orientation": "VERTICAL",
      "BarsArrangement": "CLUSTERED",
      "DataLabels": {
        "Visibility": "VISIBLE",
        "Overlap": "DISABLE_OVERLAP"
      }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```


### `tbl-93` — TableVisual — Top Delayed Orders [insight]

Detail table — top delayed orders. `GroupBy` material + plant; `Values` = `MAX(delay_days)` with a `days` suffix.

```json
{
  "TableVisual": {
    "VisualId": "tbl-93",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": {
        "PlainText": "Top Delayed Orders [insight]"
      }
    },
    "Subtitle": {
      "Visibility": "VISIBLE"
    },
    "ChartConfiguration": {
      "FieldWells": {
        "TableAggregatedFieldWells": {
          "GroupBy": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-90",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "material_name"
                }
              }
            },
            {
              "CategoricalDimensionField": {
                "FieldId": "d-91",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "plant_name"
                }
              }
            }
          ],
          "Values": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-92",
                "Column": {
                  "DataSetIdentifier": "delivery",
                  "ColumnName": "delay_days"
                },
                "AggregationFunction": {
                  "SimpleNumericalAggregation": "MAX"
                },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Suffix": "days",
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": {
                          "Symbol": "COMMA",
                          "Visibility": "VISIBLE"
                        }
                      },
                      "DecimalPlacesConfiguration": {
                        "DecimalPlaces": 0
                      }
                    }
                  }
                }
              }
            }
          ]
        }
      },
      "SortConfiguration": {}
    },
    "Actions": []
  }
}
```


### `deliv-sheet` — grid layout (`Layouts`)

The 36-column `GridLayout`. Each element ties a `VisualId` to a `ColumnIndex`/`ColumnSpan`/`RowIndex`/`RowSpan`. Verify `ColumnIndex + ColumnSpan <= 36` and no overlap (`dashboard-patterns.md` §9).

```json
[
  {
    "Configuration": {
      "GridLayout": {
        "Elements": [
          {
            "ElementId": "kpi-68",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 12,
            "RowIndex": 0,
            "RowSpan": 8
          },
          {
            "ElementId": "kpi-70",
            "ElementType": "VISUAL",
            "ColumnIndex": 12,
            "ColumnSpan": 12,
            "RowIndex": 0,
            "RowSpan": 4
          },
          {
            "ElementId": "kpi-72",
            "ElementType": "VISUAL",
            "ColumnIndex": 24,
            "ColumnSpan": 12,
            "RowIndex": 0,
            "RowSpan": 4
          },
          {
            "ElementId": "kpi-74",
            "ElementType": "VISUAL",
            "ColumnIndex": 12,
            "ColumnSpan": 12,
            "RowIndex": 4,
            "RowSpan": 4
          },
          {
            "ElementId": "kpi-76",
            "ElementType": "VISUAL",
            "ColumnIndex": 24,
            "ColumnSpan": 12,
            "RowIndex": 4,
            "RowSpan": 4
          },
          {
            "ElementId": "bar-79",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 18,
            "RowIndex": 8,
            "RowSpan": 9
          },
          {
            "ElementId": "line-83",
            "ElementType": "VISUAL",
            "ColumnIndex": 18,
            "ColumnSpan": 18,
            "RowIndex": 8,
            "RowSpan": 9
          },
          {
            "ElementId": "bar-86",
            "ElementType": "VISUAL",
            "ColumnIndex": 0,
            "ColumnSpan": 12,
            "RowIndex": 17,
            "RowSpan": 9
          },
          {
            "ElementId": "bar-89",
            "ElementType": "VISUAL",
            "ColumnIndex": 12,
            "ColumnSpan": 12,
            "RowIndex": 17,
            "RowSpan": 9
          },
          {
            "ElementId": "tbl-93",
            "ElementType": "VISUAL",
            "ColumnIndex": 24,
            "ColumnSpan": 12,
            "RowIndex": 17,
            "RowSpan": 9
          }
        ]
      }
    }
  }
]
```


`ContentType`: `INTERACTIVE`


---

# Part C — Definition tail & publish options

## `CalculatedFields` and `ParameterDeclarations`

Both empty in this dashboard — all derived metrics were pushed **upstream into
the marts** (the preferred place: compute once in ETL, not per-dashboard).

```json
{
  "CalculatedFields": [],
  "ParameterDeclarations": []
}
```

## `FilterGroups` — one date filter per sheet

**Pattern #10.** Three `TimeRangeFilter` groups, each scoped to a single
sheet's `ALL_VISUALS`. Each filters a different date column on a different
dataset (`line-daily.production_date`, `quality-daily.production_date`,
`delivery.planned_finish`). `CrossDataset: SINGLE_DATASET` because each group
touches only its own dataset. `NullOption: ALL_VALUES` is required on filters.

```json
[
  {
    "FilterGroupId": "fg-95",
    "Filters": [
      {
        "TimeRangeFilter": {
          "FilterId": "flt-94",
          "Column": {
            "DataSetIdentifier": "line-daily",
            "ColumnName": "production_date"
          },
          "IncludeMinimum": true,
          "IncludeMaximum": true,
          "NullOption": "ALL_VALUES",
          "TimeGranularity": "DAY"
        }
      }
    ],
    "ScopeConfiguration": {
      "SelectedSheets": {
        "SheetVisualScopingConfigurations": [
          {
            "SheetId": "eff-sheet",
            "Scope": "ALL_VISUALS"
          }
        ]
      }
    },
    "Status": "ENABLED",
    "CrossDataset": "SINGLE_DATASET"
  },
  {
    "FilterGroupId": "fg-97",
    "Filters": [
      {
        "TimeRangeFilter": {
          "FilterId": "flt-96",
          "Column": {
            "DataSetIdentifier": "quality-daily",
            "ColumnName": "production_date"
          },
          "IncludeMinimum": true,
          "IncludeMaximum": true,
          "NullOption": "ALL_VALUES",
          "TimeGranularity": "DAY"
        }
      }
    ],
    "ScopeConfiguration": {
      "SelectedSheets": {
        "SheetVisualScopingConfigurations": [
          {
            "SheetId": "qual-sheet",
            "Scope": "ALL_VISUALS"
          }
        ]
      }
    },
    "Status": "ENABLED",
    "CrossDataset": "SINGLE_DATASET"
  },
  {
    "FilterGroupId": "fg-99",
    "Filters": [
      {
        "TimeRangeFilter": {
          "FilterId": "flt-98",
          "Column": {
            "DataSetIdentifier": "delivery",
            "ColumnName": "planned_finish"
          },
          "IncludeMinimum": true,
          "IncludeMaximum": true,
          "NullOption": "ALL_VALUES",
          "TimeGranularity": "DAY"
        }
      }
    ],
    "ScopeConfiguration": {
      "SelectedSheets": {
        "SheetVisualScopingConfigurations": [
          {
            "SheetId": "deliv-sheet",
            "Scope": "ALL_VISUALS"
          }
        ]
      }
    },
    "Status": "ENABLED",
    "CrossDataset": "SINGLE_DATASET"
  }
]
```

## `DashboardPublishOptions`

Reader-facing capabilities. Note ad-hoc filtering, CSV export, drill up/down,
and the newer Quick Suite options (`ExecutiveSummaryOption`,
`DataStoriesSharingOption`, `QuickSuiteActionsOption`) are all enabled;
`DataQAEnabledOption` and `ExportWithHiddenFieldsOption` are off. This object is
passed alongside the `Definition` to `create-dashboard`.

```json
{
  "AdHocFilteringOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "ExportToCSVOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "SheetControlsOption": {
    "VisibilityState": "COLLAPSED"
  },
  "SheetLayoutElementMaximizationOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "VisualMenuOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "VisualAxisSortOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "ExportWithHiddenFieldsOption": {
    "AvailabilityStatus": "DISABLED"
  },
  "DataPointDrillUpDownOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "DataPointMenuLabelOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "DataPointTooltipOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "DataQAEnabledOption": {
    "AvailabilityStatus": "DISABLED"
  },
  "QuickSuiteActionsOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "ExecutiveSummaryOption": {
    "AvailabilityStatus": "ENABLED"
  },
  "DataStoriesSharingOption": {
    "AvailabilityStatus": "ENABLED"
  }
}
```


---

# Part D — How to adapt this to a different domain

This is a manufacturing dashboard, but the **shapes are domain-agnostic**. To
retarget it (e.g. retail, healthcare, finance), change data references — not
structure. Work top-down:

### 1. Dataset ARNs and identifiers (`DataSetIdentifierDeclarations`)
- Replace every `DataSetArn` (`arn:aws:quicksight:us-east-1:730335655603:dataset/hansung-*`)
  with your account, region, and dataset IDs.
- Rename each `Identifier` to your domains (e.g. `eff-kpi` → `sales-kpi`,
  `line-daily` → `store-daily`). The identifier is referenced by every visual's
  `Column.DataSetIdentifier`, so rename consistently (a global find/replace per
  identifier is safest).
- Keep the **single-row-KPI vs grain-level split**: one `*-kpi` dataset per
  sheet for the cards, plus grain marts for trend/ranking/detail.

### 2. Column names (every `Column.ColumnName`)
- Swap manufacturing columns for yours: `utilization_pct` → `conversion_rate`,
  `defect_rate_pct` → `return_rate`, `material_name` → `product_name`,
  `production_date` → `order_date`, `consumption_cost_kwon` → `revenue_usd`, etc.
- Match the **aggregation to the column's grain**: single-row KPI columns keep
  `MIN`; grain-level measures use `SUM`/`AVERAGE`/`COUNT` as appropriate.
- STRING keys counted with `COUNT` must stay in a `CategoricalMeasureField`
  (pattern #6) — don't move them to `NumericalMeasureField`.

### 3. Titles, suffixes/prefixes, currency
- Retitle visuals (`FormatText.PlainText`) and sheet `Name`s in your language.
- Replace unit suffixes (`sec`, `EA`, `cases`, `days`, `%`) and the `₩` prefix /
  `NumberScale: AUTO` with your locale's units and currency.

### 4. Targets, thresholds, reference lines
- The gauge `TargetValues` column, the `85.0` reference line, and every
  conditional-formatting threshold (`> 2.5`, `>= 80`, `> 400`) are
  **domain-specific** — set them from the customer's real targets (`SKILL.md`
  Q10). If there is no real target, **drop the gauge/reference line** rather
  than inventing one (`dashboard-patterns.md` §3).

### 5. Filters
- Repoint each `TimeRangeFilter` to your date column + dataset, and re-scope to
  your `SheetId`s.

### 6. Layout
- Keep the `GridLayout` element list but remap `ElementId`s to your visual IDs.
  Re-verify `ColumnIndex + ColumnSpan <= 36` and no overlaps after any change
  (`dashboard-patterns.md` §9).

### 7. Theme / IDs
- `ThemeArn`, `DashboardId`, `Name`, and all `VisualId`/`FieldId` strings are
  free-form — set `ThemeArn` to your theme (or omit for the default), and keep
  IDs unique within the definition. `FieldId`s only need to be unique per visual
  and to match between the field well and any `SortConfiguration`/conditional
  reference.

> **Workflow tip (`dashboard-patterns.md` §1):** for > 5 visuals, build in the
> Quick Sight UI, then `describe-dashboard-definition` and check the export into
> CDK — exactly how *this* file was produced. Don't hand-author large
> definitions from scratch.

---

# Part E — Known improvements (now with copy-paste patches)

This definition is deployed, STRICT-clean, and renders — so **the JSON in Parts
A–C above is left exactly as exported, byte-faithful, not modified.** That is
deliberate: the whole value of this file is that it is a *real, validated answer
key*, and the only way to keep that guarantee is to not hand-edit it without
re-running STRICT (`dashboard-patterns.md` §4) against the live service.

What changed in this revision: the items below were prose-only "you could…"
notes; each now ships a **concrete, schema-checked JSON patch** you can drop in.
Every patch was validated property-by-property against the Quick Sight API
Reference (`describe`/`create-dashboard` definition schema). They are **patches
for the next build**, applied on top of a copy — not edits to the answer-key
JSON above. After applying any of them, run the §4 STRICT probe before deploy.

> **Patch convention.** Each block shows only the keys to **add or change**
> inside the named visual's `ChartConfiguration` (or the visual root, for
> `Subtitle`/`ConditionalFormatting`). Merge them into the existing object;
> don't replace sibling keys. New `FieldId`s use a `-px`/`-cf` suffix so they
> can't collide with the export's `m-NN`/`d-NN` scheme.

### E1 — Fill or hide every empty `Subtitle`

Every visual ships `"Subtitle": {"Visibility": "VISIBLE"}` with no text, which
leaves a dead caption slot. Either give it one line (`FormatText.PlainText`, a
`LongFormatText`) or hide it. Example for `kpi-5` (Average Cycle Time):

```json
{
  "Subtitle": {
    "Visibility": "VISIBLE",
    "FormatText": { "PlainText": "Last 30 days · line average" }
  }
}
```

…or, to remove the slot entirely:

```json
{ "Subtitle": { "Visibility": "HIDDEN" } }
```

### E2 — Heatmap (`heat-19`): single-hue `ColorScale` + labels + legend

The export's heatmap has no `ColorScale`, `DataLabels`, or `Legend`, so it falls
back to Quick's default ramp. A sequential `GRADIENT` scale (`ColorScale` →
`ColorFillType: GRADIENT`, 2–3 `DataColor`s light→dark — utilization is *ordered
magnitude*, so one hue, not a categorical palette) plus row/column labels makes
the gradient readable. Merge into `heat-19`'s `ChartConfiguration`:

```json
{
  "ColorScale": {
    "ColorFillType": "GRADIENT",
    "Colors": [
      { "Color": "#FDE7E9", "DataValue": 60.0 },
      { "Color": "#FFF4CE", "DataValue": 75.0 },
      { "Color": "#1F6FEB", "DataValue": 100.0 }
    ],
    "NullValueColor": { "Color": "#EBEDF0" }
  },
  "DataLabels": { "Visibility": "VISIBLE", "Overlap": "DISABLE_OVERLAP" },
  "Legend": { "Visibility": "VISIBLE", "Position": "RIGHT" },
  "RowLabelOptions": { "Visibility": "VISIBLE" },
  "ColumnLabelOptions": { "Visibility": "VISIBLE" }
}
```

### E3 — Tables (`tbl-93`, `tbl-66`): sort, paginate, conditional-format

`tbl-93` is titled "Top Delayed Orders" (top delayed orders) but ships an empty
`SortConfiguration`, so "top" is arbitrary. Sort by `delay_days` DESC and bound
the page. Note tables use `RowSort` (NOT `CategorySort`) and
`PaginationConfiguration`. Merge into `tbl-93`'s `ChartConfiguration`:

```json
{
  "SortConfiguration": {
    "RowSort": [
      { "FieldSort": { "FieldId": "m-92", "Direction": "DESC" } }
    ],
    "PaginationConfiguration": { "PageSize": 20, "PageNumber": 0 }
  }
}
```

`tbl-66` (Standard vs Actual Cost Comparison Table) can flag over-budget rows. Tables take a
top-level `ConditionalFormatting` (`TableVisual.ConditionalFormatting`, verified
present on `TableVisual`, not just pivot tables) → `Cell` → `TextFormat`. Add to
the `tbl-66` visual root (sibling of `ChartConfiguration`):

```json
{
  "ConditionalFormatting": {
    "ConditionalFormattingOptions": [
      {
        "Cell": {
          "FieldId": "m-65",
          "TextFormat": {
            "TextColor": {
              "Solid": {
                "Expression": "AVERAGE({variance_pct}) > 5",
                "Color": "#D1242F"
              }
            }
          }
        }
      }
    ]
  }
}
```

### E4 — Document the single-row-KPI `MIN` intent

The single-row KPI cards use `MIN` aggregation, which is *correct* (one row, so
MIN=MAX=SUM=AVG) but reads oddly. There is no functional change to make here —
the fix is **documentation**: pattern #1 at the top of this file and §10 of
`dashboard-patterns.md` already explain it. Leave the `MIN`; don't "fix" it to
`SUM` thinking it's a bug (that's how the 8.3× overcount got introduced on a
*multi*-row mart — the lesson is the dataset shape, not the function).

### E5 — Native `InsightVisual` for the `[insight]` tiles

Sheets tag insight tiles with `[insight]` in the title but use ordinary
charts. A native `InsightVisual` with a computed narrative would lift them — but
`CustomNarrative.Narrative` must be **valid XML**, not plain text, or it fails
with `Content not allowed in prolog` (§3), and `TopBottomRanked` requires
`ResultSize`. Because this is a §3 "probe-first" visual type, build it in the UI,
export, and paste the validated shape rather than hand-authoring it here.

### E6 — Pie (`pie-34`): bound the slices with a TOP-N + Other

If defect types are many, an unbounded donut turns to confetti. `PieChartSort
Configuration` accepts `CategoryItemsLimit`; keep `OtherCategories: INCLUDE` to
roll the tail into a "Other" slice (vs `EXCLUDE`, which drops it). Merge into
`pie-34`'s `SortConfiguration`:

```json
{
  "SortConfiguration": {
    "CategorySort": [
      { "FieldSort": { "FieldId": "m-33", "Direction": "DESC" } }
    ],
    "CategoryItemsLimit": { "ItemsLimit": 7, "OtherCategories": "INCLUDE" }
  }
}
```

### E7 — Reference lines on the quality / delivery trends

Only `line-15` (utilization) has a target line. The quality trend (`line-41`)
and delivery trend (`line-83`) have business targets too. Patch for `line-41`
(defect-rate target 2.5%) — same `ReferenceLines` shape as `line-15`, merged
into its `ChartConfiguration`:

```json
{
  "ReferenceLines": [
    {
      "Status": "ENABLED",
      "DataConfiguration": {
        "StaticConfiguration": { "Value": 2.5 },
        "AxisBinding": "PRIMARY_YAXIS",
        "SeriesType": "LINE"
      },
      "StyleConfiguration": { "Pattern": "DASHED", "Color": "#D1242F" },
      "LabelConfiguration": {
        "CustomLabelConfiguration": { "CustomLabel": "Defect rate target 2.5%" },
        "FontColor": "#D1242F",
        "HorizontalPosition": "RIGHT",
        "VerticalPosition": "ABOVE"
      }
    }
  ]
}
```

Use the customer's real target, not 2.5 — and if there is no real target,
**omit the line** (§0 Q10 / §3: never invent one).

### E8 — `ParameterDeclarations` + a control

`CalculatedFields` and `ParameterDeclarations` are both empty and
`SheetControlsOption` is only `COLLAPSED`. A line/plant dropdown control on top
of a `StringParameterDeclaration` makes the dashboard interactive beyond ad-hoc
filtering. This is a multi-part change (declaration + control + filter wiring)
best done in the UI then exported (§1). Skeleton of the declaration:

```json
{
  "ParameterDeclarations": [
    {
      "StringParameterDeclaration": {
        "ParameterValueType": "MULTI_VALUED",
        "Name": "LineCode",
        "ValueWhenUnset": { "ValueWhenUnsetOption": "NULL" }
      }
    }
  ]
}
```

> None of these block deployment. They are the gap between "passes STRICT +
> renders" and "fantastic," per the feedback that motivated this reference.

---

# Part F — Research-driven enhancements (best-in-class manufacturing BI)

Parts A–E keep the dashboard *correct and clean*. This part is about making it
*best-in-class*, drawn from manufacturing-BI conventions (OEE/TPM benchmarks,
Pareto for cause analysis) and data-viz canon (sequential vs categorical color,
gray-for-context, Korean myriad units). These are **net-new visuals/patterns**,
all schema-verified; add via the UI-first → export flow (§1) for anything bigger
than a single field-well.

### F1 — A defect-cause **Pareto** (the quality dashboard's signature visual)

The quality sheet has a donut (`pie-34`) and a TOP-10 bar (`bar-37`) for defect
volume, but no Pareto — the chart that answers "which few causes drive most of
the defects?" Build it as a `ComboChartVisual`: defect-count **bars** sorted
DESC on the primary axis + a cumulative-% **line** on the **secondary** axis,
with an 80% reference line marking the vital few.

Prerequisite — a cumulative-% measure. Add it as a `CalculatedField` on
`defect-cause` (functions verified — `runningSum`, `percentOfTotal`):
```json
{
  "Name": "cumulative_defect_ratio",
  "DataSetIdentifier": "defect-cause",
  "Expression": "runningSum(percentOfTotal(sum({mes_defect_qty})), [percentOfTotal(sum({mes_defect_qty})) DESC])"
}
```

**Axis binding — you do NOT set it per-series in a combo chart.** In a
`ComboChartVisual`, `BarValues` render on the **primary** Y-axis and `LineValues`
on the **secondary** Y-axis *automatically* — there is no per-field `AxisBinding`
to set. (Per-series `AxisBinding` exists only on **line** charts, via
`DataFieldSeriesItem.AxisBinding`; the combo-chart series item
`DataFieldComboSeriesItem` carries a `Settings` of type
`ComboChartSeriesSettings`, whose only members are
`BorderSettings`/`DecalSettings`/`LineStyleSettings`/`MarkerStyleSettings` — **no
`AxisBinding`**.) So the Pareto cumulative-% line lands on the secondary axis just
by being a `LineValue`. Use `SecondaryYAxisLabelOptions` /
`SecondaryYAxisDisplayOptions` to *label and scale* that axis (0–100%), and bind
the 80% reference line to `SECONDARY_YAXIS` — but do **not** add a `Series` block
to "move it" there.

```json
{
  "ComboChartVisual": {
    "VisualId": "pareto-defect",
    "Title": {
      "Visibility": "VISIBLE",
      "FormatText": { "PlainText": "Defect-cause Pareto (cumulative 80%)" }
    },
    "Subtitle": { "Visibility": "HIDDEN" },
    "ChartConfiguration": {
      "FieldWells": {
        "ComboChartAggregatedFieldWells": {
          "Category": [
            {
              "CategoricalDimensionField": {
                "FieldId": "d-px1",
                "Column": { "DataSetIdentifier": "defect-cause", "ColumnName": "defect_name" }
              }
            }
          ],
          "BarValues": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-px2",
                "Column": { "DataSetIdentifier": "defect-cause", "ColumnName": "mes_defect_qty" },
                "AggregationFunction": { "SimpleNumericalAggregation": "SUM" },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "SeparatorConfiguration": {
                        "ThousandsSeparator": { "Symbol": "COMMA", "Visibility": "VISIBLE" }
                      },
                      "DecimalPlacesConfiguration": { "DecimalPlaces": 0 }
                    }
                  }
                }
              }
            }
          ],
          "LineValues": [
            {
              "NumericalMeasureField": {
                "FieldId": "m-px3",
                "Column": { "DataSetIdentifier": "defect-cause", "ColumnName": "cumulative_defect_ratio" },
                "AggregationFunction": { "SimpleNumericalAggregation": "MAX" },
                "FormatConfiguration": {
                  "FormatConfiguration": {
                    "NumberDisplayFormatConfiguration": {
                      "Suffix": "%",
                      "DecimalPlacesConfiguration": { "DecimalPlaces": 0 }
                    }
                  }
                }
              }
            }
          ],
          "Colors": []
        }
      },
      "SortConfiguration": {
        "CategorySort": [
          { "FieldSort": { "FieldId": "m-px2", "Direction": "DESC" } }
        ],
        "CategoryItemsLimit": { "ItemsLimit": 10, "OtherCategories": "EXCLUDE" }
      },
      "BarsArrangement": "CLUSTERED",
      "SecondaryYAxisDisplayOptions": {
        "DataOptions": {
          "NumericAxisOptions": {
            "Range": {
              "MinMax": { "Minimum": 0.0, "Maximum": 100.0 }
            }
          }
        }
      },
      "SecondaryYAxisLabelOptions": {
        "Visibility": "VISIBLE",
        "AxisLabelOptions": [
          {
            "CustomLabel": "Cumulative ratio",
            "ApplyTo": {
              "FieldId": "m-px3",
              "Column": { "DataSetIdentifier": "defect-cause", "ColumnName": "cumulative_defect_ratio" }
            }
          }
        ]
      },
      "ReferenceLines": [
        {
          "Status": "ENABLED",
          "DataConfiguration": {
            "StaticConfiguration": { "Value": 80.0 },
            "AxisBinding": "SECONDARY_YAXIS",
            "SeriesType": "LINE"
          },
          "StyleConfiguration": { "Pattern": "DOTTED", "Color": "#57606A" },
          "LabelConfiguration": {
            "CustomLabelConfiguration": { "CustomLabel": "80%" },
            "FontColor": "#57606A",
            "HorizontalPosition": "RIGHT"
          }
        }
      ],
      "BarDataLabels": { "Visibility": "VISIBLE", "Overlap": "DISABLE_OVERLAP" },
      "LineDataLabels": { "Visibility": "VISIBLE", "Overlap": "DISABLE_OVERLAP" }
    },
    "Actions": [],
    "ColumnHierarchies": []
  }
}
```

> **No `Series` block is needed (or valid) here.** Verified against the API
> Reference: `DataFieldComboSeriesItem.Settings` is a `ComboChartSeriesSettings`,
> whose only members are `BorderSettings`/`DecalSettings`/`LineStyleSettings`/
> `MarkerStyleSettings` — there is **no `AxisBinding`**. A combo chart always puts
> `BarValues` on the primary axis and `LineValues` on the secondary, so the
> cumulative-% line is on the right axis automatically. (Per-series `AxisBinding`
> exists only on **line** charts, via `DataFieldSeriesItem`.) Control the
> secondary axis only through `SecondaryYAxisDisplayOptions` (range/scale, pinned
> to 0–100% above) and `SecondaryYAxisLabelOptions` (label) — both verified
> members of `ComboChartConfiguration`.

### F2 — Myriad (man/eok) units on money cards (not B/M/K)

`kpi-47`/`kpi-51` and the cost bars use `NumberScale: AUTO`, which renders
`₩1.2B` / `₩340M`. CJK/Korean groups by 4 digits (man 10⁴, eok 10⁸, jo 10¹²), so a
CJK/Korean reader parses `man/eok`, not `B/M/K` (Microsoft globalization, ko-KR). Quick
has no native man/eok scale, so divide upstream and label the unit. `CalculatedField`
on the single-row `cost-kpi` (keep `MIN`, per pattern #1):

```json
{
  "Name": "total_consumption_cost_100m",
  "DataSetIdentifier": "cost-kpi",
  "Expression": "min({total_consumption_cost_kwon}) / 100000000"
}
```

Then point `kpi-47`'s value well at `total_consumption_cost_100m`, drop `NumberScale`, set 1
decimal, and put the unit in the title (`Total Consumption Cost (100M KRW)`). The card reads
`12.4` under a `(100M KRW)` header instead of `₩1.2B`.

### F3 — Color discipline (sequential vs categorical; gray for context)

- **Heatmaps / gauges → single-hue sequential** (`ColorScale GRADIENT`, one hue
  light→dark) — applied in E2. Ordered magnitude must not use a categorical
  multi-hue palette.
- **Status (Utilization vs Target, Defect Rate, Delivery) → traffic-light** red/amber/green, and
  *only* there — already done correctly on `kpi-9/68/72` via aggregation-expr
  conditional formatting.
- **Everything else → 2–3 base hues + gray.** Render secondary/context series
  and non-data ink in gray so the eye lands on the focal metric. Set the
  categorical array and the `minMaxGradient` ramp on the **theme**
  (`DataColorPalette` in `CreateTheme`, see `quicksight-cdk.md`), so colors stay
  consistent and stable across sort/filter (field-based coloring) rather than
  per-visual.

### F4 — OEE framing for the efficiency sheet

The efficiency sheet leads with utilization but not the full **OEE =
Availability × Performance × Quality** decomposition that manufacturing readers
expect. If the marts can supply the three factors, a KPI row of A/P/Q each with a
`TargetValues` well (world-class **A 90% · P 95% · Q 99.9%**, composite **OEE
85%**; Nakajima/TPM) turns the sheet from "one gauge" into the standard OEE
cockpit. Use the customer's real targets where known; treat the TPM numbers as
illustrative defaults, never as invented customer data (§0 Q10).

### F5 — Further best-in-class techniques (all verified in the definition schema)

Beyond the patches above, these are the highest-leverage additions the AWS demo
gallery / showcase dashboards use that this dashboard doesn't yet — each is a
real member of the definition schema (not a theme/UI-only feature), so it can go
straight into the JSON:

- **Inline table sparklines + data bars.** A `TableVisual` can carry a trend
  column rendered as a sparkline and a magnitude column rendered as in-cell data
  bars — so `tbl-66`/`tbl-93` show shape *and* size at a glance without a
  separate chart. (Table sparklines cap at 3 columns / 52 points each.)
  Docs: https://docs.aws.amazon.com/quick/latest/userguide/format-sparklines.html
- **Dynamic reference lines.** `line-15`/`line-41` use a *static* target. A
  `ReferenceLine` can instead be **data-driven** (`DynamicConfiguration` →
  computed `AVERAGE`/percentile of the measure) so the line auto-recomputes as
  filters change — a live "vs. average" band rather than a hardcoded number.
  Use this only where a *computed* benchmark is meaningful; for a fixed business
  target the static line is still correct (and never invent one — §0 Q10).
  Docs: https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-quicksight-dashboard-referencelinedataconfiguration.html
- **KPI primary-value icon (not just text color).** The KPI's
  `ConditionalFormatting` → `PrimaryValue` block takes an `Icon`
  (`ConditionalFormattingIcon` — icon set or custom Unicode) alongside the
  `TextColor` rule already used on `kpi-9/68/72` — a ▲/▼ or status dot beside the
  number reads faster than color alone (and survives color-blind viewers).
  Docs: https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-quicksight-analysis-kpiprimaryvalueconditionalformatting.html
- **Small multiples** for per-line/-plant trends — add the split dimension to a
  chart's `SmallMultiples` field well instead of overplotting one line chart with
  many series (cleaner than the multi-series `line-41`/`line-83` when the series
  count is high). Docs: https://docs.aws.amazon.com/quick/latest/userguide/small-multiples.html

**Achievable-but-elsewhere (do NOT try to author in `--definition`):** palette,
card styling (border/opacity/corner-radius/padding), sheet-background gradient,
and typography live in the **theme** (`CreateTheme`/`UpdateTheme` — two API
calls, a `ThemeArn` reference in the definition), per F3. And the generative-BI
layer — Executive Summaries, Data Stories, Q&A/Scenarios, "build with natural
language" — is an **author/runtime** feature enabled on publish (and Pro-tier /
Region-gated), **not** a node you write in the definition JSON; the
`ExecutiveSummaryOption`/`DataStoriesSharingOption`/`DataQAEnabledOption` toggles
in `DashboardPublishOptions` (Part C) only *enable* them.

**Schema caveats worth knowing (so you don't promise the impossible):**
- **Heatmaps have no conditional formatting** — coloring is *only* the
  `ColorScale` (gradient/discrete, max 3 stops). The E2 patch is the ceiling for
  `heat-19`; rule-based per-cell colors aren't available.
  Docs: https://docs.aws.amazon.com/quick/latest/userguide/heat-map.html
- **KPI conditional formatting reaches only the primary value** (text + icon) and
  the progress-bar color — the comparison/trend values can't be conditionally
  formatted.

> Sources behind this part: AWS Quick Sight demo gallery + BI/big-data showcase
> blogs (conditional formatting, free-form/section layouts, field-based coloring,
> small multiples, sparklines, Quick Suite); oee.com / TPM world-class OEE
> benchmarks; Microsoft globalization (ko-KR myriad grouping); Knaflic/Few on
> sequential-vs-categorical color and gray-for-context. Every JSON property in
> Parts E–F was checked member-by-member against the Quick Sight definition API
> Reference — including the F1 correction (combo charts have no per-series
> `AxisBinding`).
