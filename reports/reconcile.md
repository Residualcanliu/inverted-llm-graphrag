# 建图对账报告

由 `scripts/reconcile.py` 生成。

## 节点

| 标签 | 实际 | 预期 | 结果 |
|---|---|---|---|
| `Location` | 17 | — | |
| `Equipment` | 127 | — | |
| `SparePart` | 84 | — | |
| `WorkOrder` | 370 | — | |
| `Process` | 6 | — | |
| `Operation` | 16 | — | |
| `SafetyMeasure` | 26 | — | |
| `FailureMode` | 65 | 检修26 + 成因39 = 65 以内 | |

## 关系

| 关系 | 实际 | 预期 | 结果 |
|---|---|---|---|
| `IN_SHOP` | 127 | 127 |  |
| `STORED_AT` | 45 | — |  |
| `FITS` | 2580 | — |  |
| `EXPERIENCED` | 370 | — |  |
| `CAUSED_BY` | 370 | — |  |
| `USED_PART` | 682 | — |  |
| `HAS_STEP` | 16 | — |  |
| `PRECEDES` | 10 | — |  |
| `USES` | 130 | — |  |
| `DEPENDS_ON` | 650 | 650 |  |
| `MITIGATED_BY` | 26 | — |  |
| `TRIGGERS` | 44 | — |  |

## 孤儿节点

合计 0


## 依赖网络覆盖

工艺流程未覆盖的设备：18 台（切割设备本就是独立工序）
