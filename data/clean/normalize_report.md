# 规范化处理报告

由 `scripts/normalize.py` 生成。记录本次读了什么、清掉了什么。

## 产物

| 文件 | 条数 |
|---|---|
| equipment.json | 127 台设备 |
| spare_parts.json | 95 条（备件 46 + 工具 49） |
| repairs.json | 370 条检修记录 |
| processes.json | 6 条流程 |
| manuals.json | 5 份手册 |

## 处理过的格式问题

源数据里的五种脏写法，都在 `app/ingest/normalize.py` 里处理：

| 问题 | 例 |
|---|---|
| 编号省略前缀 | `tenlong-001、002、005、006` |
| 区间省略前缀 | `chengxin-001～006、025～030` |
| 「通用」后缀 | `tenlong/yuelong通用` |
| 分隔符不统一 | `、` `，` `/` |
| 规格嵌在名称里 | `硬质合金车刀片（CNMG120408）` |

## 台数校验

工艺流程图每格标了台数，展开后逐一核对。

```
流程1 车削（tenlong系列） 4 台
流程1 磨削（chengxin系列） 12 台
流程2 车削（tenlong系列） 4 台
流程2 铣削（yuelong系列） 8 台
流程2 磨削（chengxin系列） 12 台
流程3 车削（tenlong系列） 3 台
流程3 焊接（huanmai系列） 6 台
流程3 磨削（chengxin系列） 12 台
流程4 铣削（yuelong系列） 6 台
流程4 焊接（huanmai系列） 6 台
流程5 车削（tenlong系列） 4 台
流程5 铣削（yuelong系列） 10 台
流程5 焊接（huanmai系列） 9 台
流程5 磨削（chengxin系列） 10 台
流程6 铣削（yuelong系列） 8 台
流程6 磨削（chengxin系列） 16 台
```

## 建图基准

第④步对账用这些数字核对。任何一项对不上，说明管线有问题。

| 指标 | 预期值 |
|---|---|
| Equipment | 127 |
| SparePart | 95 |
| WorkOrder | 370 |
| Process | 6 |
| Operation | 16 |
| Location | 17 |
| DEPENDS_ON | 650 |

## 数据特征

- 检修记录带日期：370/370
- 高危工具：5 项
- 库存低于安全线的备件：0 项
- 有依赖边的设备：109 台

## 已知缺口

- 切割设备（heyue）不在工艺流程里，源文档说明它是独立下料工序，因此在依赖网络上孤立。
- 故障成因列是自由文本，里面的名字（如「磨粒钝化」「熔渣堵塞」）多数不在故障词表中，建 TRIGGERS 边需要人工映射。
