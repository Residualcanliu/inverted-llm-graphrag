# inverted-llm-graphrag

大模型只写查询语句，取数和计算交给图数据库。

通常的 RAG 做法是：检索片段 → 大模型读全文 → 回答。这个项目试另一种分工：
大模型把自然语言翻译成 Cypher，真正取数和计算的是 Neo4j。
项目里管它叫**倒置LLM**——把大模型从「推理者」降级成「翻译器」。

做这个是为了回答一个问题：**这套架构的优势边界在哪类问题上。**
不是一个笼统的准确率提升数字，理由见[第五节](#五对照实验怎么设计的)。

---

## 目录

- [一、要解决的问题](#一要解决的问题)
- [二、一条查询是怎么走的](#二一条查询是怎么走的)
- [三、图谱长什么样](#三图谱长什么样)
- [四、三层防线](#四三层防线)
- [五、对照实验怎么设计的](#五对照实验怎么设计的)
- [六、跑起来](#六跑起来)
- [七、现在做到哪了](#七现在做到哪了)
- [八、文档与参考](#八文档与参考)

---

## 一、要解决的问题

传统 RAG 有两种失效方式，性质不同。

**信息装不下.** 文档多到一定程度，检索只能取回部分片段，模型基于残缺信息推理，答案就偏。
这类失效随语料规模增大而加剧。

**答案不在文本里.** 有一类问题，答案是从结构里算出来的，不写在任何一句话上。
把全部文档塞进上下文也答不对。

区分这两类，用一个不依赖实现细节的判据：

> 把全部文档塞进上下文，交给一个聪明人。他能答对吗？
>
> - 能答对 → **A 类**。瓶颈在「看到」。
> - 他得拿纸笔算、或者画个图才答得对 → **B 类**。瓶颈在「算」。

B 类有五种：

| 类型 | 问法示例 | 传统 RAG 卡在哪 | 图怎么做 |
|---|---|---|---|
| 多跳依赖 | 3号泵停机影响哪些设备？ | 信息分散在不同页，要做传递闭包，跳数一多必漏 | 变长路径 `-[:DEPENDS_ON*1..5]->` |
| 聚合统计 | 6号机组的 MTBF 是多少？ | 要遍历工单算时间间隔求均值 | 聚合函数，精确可复现 |
| 否定与补集 | 哪些设备没配安全措施？ | 「没有」在文档里不作为句子存在 | `WHERE NOT (e)-[:X]->()` |
| 排序与关键性 | 按重要度给设备排序 | 「重要度」是从拓扑算出来的，文本里没有这个字段 | PageRank 等图算法 |
| 根因追溯 | 追溯工单的事件链 | 要沿故障链完整走一条路径，断一环也不知道 | 路径查询 |

第四种最能说明问题。**「重要度」这个信息在文档里压根不存在**，读多少页都没有。

---

## 二、一条查询是怎么走的

拿一道真实跑过的题走一遍。问题：

> 更换3号泵的机械密封需要什么资质？

### 1. 组装 prompt

prompt 分四块：角色与规则、图谱 schema、输出要求、few-shot 示例。
schema 是从 Neo4j 实时读的（`db.labels()` 那几个过程），不硬编码——改图不用改代码。

示例那一段是关键，后面会讲到为什么。

### 2. 模型生成 Cypher

发给 `qwen2.5-coder:14b`，温度 0.1，只让它续写 `Cypher：` 后面那段。
它返回：

```cypher
MATCH (e:Equipment {name:'3号泵'})-[:HAS_OPERATION]->(o:Operation)
      -[:REQUIRES_ROLE]->(r:Role)
RETURN DISTINCT r.cert AS 资质
```

模型经常不老实——加解释、套 Markdown 围栏、输出 ` thinking` 块。
所以拿回来先做三步清理：剥 think 块、剥围栏、裁掉查询前面的说明文字。

### 3. 四道静态校验（本地，不连数据库）

| 检查 | 这道题的情况 |
|---|---|
| 结构 | 以 `MATCH` 开头 ✓ |
| 只读 | 没有 `CREATE`/`DELETE`/`SET` ✓ |
| schema 一致性 | `Equipment`/`Operation`/`Role` 都在，`name`/`cert` 属性都在 ✓ |
| 关系方向 | `REQUIRES_ROLE` 规定是 `Operation → Role`，查询里就是这么写的 ✓ |

这一步是纯正则匹配，微秒级。**它不理解查询要干什么，只看文本。**

### 4. EXPLAIN 预检（连数据库，但不执行）

把查询交给 Neo4j 编译成执行计划，然后丢掉计划，一个数据都不碰。
跟 SQL 的 `EXPLAIN` 是同一个机制。

### 5. 执行

用 `READ_ACCESS` 模式发过去，Neo4j 真的去图里找节点、沿关系遍历、做计算，
返回结果集。这个模式下**服务端拒绝一切写操作**。

整条链路约 0.6 秒。

### 出错了会怎样

同一道题，模型有时候会把「机械密封」当成查询目标——它在 schema 里是个 `SparePart`。
于是写出这种查询：

```cypher
MATCH (e:Equipment)-[:HAS_OPERATION]->(o:Operation)
      -[:USES_PART]->(p:SparePart {name:'机械密封'})-[:SUPPLIED_BY]->(s:Supplier)
RETURN DISTINCT s.name AS 供应商
```

**语法全对，能执行，返回非空结果——只是答非所问。**

这次是静态校验拦住了（`REQUIRES_ROLE` 挂错了位置）。拦截后把**完整的错误信息**
回喂给模型重写，最多两轮。实测：

```
第一轮  RETURN EXISTS(f2) AS 影响
报错    Argument to EXISTS(...) is not a pattern (line 2, column 15)
第二轮  RETURN count(f2) > 0 AS 影响     ← 通过
```

**回喂的必须是错误原文，不能只给错误代号。** 早期版本只回喂
`Neo.ClientError.Statement.SyntaxError`，模型看到「语法错」三个字不知道该改什么，
原样重写了两遍，两轮修复全白费。

---

## 三、图谱长什么样

10 类节点、15 类关系。

**节点**：`Location` `Equipment` `Operation` `Role` `SparePart` `Supplier`
`Hazard` `SafetyMeasure` `WorkOrder` `FailureMode`

**关系**：

```
(Location)-[:CONTAINS]->(Equipment)
(Equipment)-[:DEPENDS_ON]->(Equipment)             设备依赖设备
(Equipment)-[:HAS_OPERATION]->(Operation)          设备有作业
(Operation)-[:REQUIRES_ROLE]->(Role)               作业需要岗位
(Operation)-[:USES_PART]->(SparePart)              作业使用备件
(Operation)-[:HAS_HAZARD]->(Hazard)                作业有风险
(Operation)-[:PRECEDES]->(Operation)               工序先后
(Hazard)-[:MITIGATED_BY]->(SafetyMeasure)          风险由措施缓解
(SparePart)-[:SUPPLIED_BY]->(Supplier)             备件供应商
(Equipment)-[:EXPERIENCED]->(WorkOrder)            设备经历过工单
(WorkOrder)-[:CAUSED_BY]->(FailureMode)            工单由故障引起
(WorkOrder)-[:USED_PART]->(SparePart)              工单用了备件
(WorkOrder)-[:PERFORMED_BY]->(Role)                工单由岗位执行
(FailureMode)-[:OCCURS_ON]->(Equipment)            故障发生于设备
(FailureMode)-[:TRIGGERS]->(FailureMode)           故障连锁引发故障
```

最后一条是刻意加的。有了故障连锁，根因追溯才是真正的多跳路径
（密封失效 → 流量不足 → 机组过热 → 停机）。去掉它，那道题退化成两三跳就断的浅查询。

完整清单和每条关系支撑哪类问题，见 [DESIGN.md 第四节](DESIGN.md)。

---

## 四、三层防线

这三层保证的是「查询合法、能执行」。**它们对「答案对不对」无能为力**，这点后面会说。

**第一层：四道静态校验**（本地正则，微秒级）

结构、只读、schema 一致性、关系方向。方向那道是重点——公开数据里
关系方向写反占了全部语义错误的 33%，是占比最高的一类。

**第二层：EXPLAIN 预检**（连库，但不执行）

抓真正的语法错误。抓不到属性名写错（Neo4j 属性是动态的，查不存在的属性返回 `null`
而不是报错）、方向反了、聚合算错。

**第三层：`READ_ACCESS` 执行**

服务端强制拒绝写入。实测拒绝范围：`CREATE` / `MERGE` / `SET` / `DELETE` /
`DETACH DELETE` / `REMOVE` 全部被拒，`apoc.create.node` 这类写过程也被拒。

> 这里本来设计的是「建一个只读数据库账号」。走不通——**Neo4j 社区版不支持 RBAC**，
> `GRANT ROLE` 直接报错，建出来的「只读账号」可以随意增删改。改用 driver 的
> `READ_ACCESS` 模式，这个在单实例上服务端会真的强制。`scripts/init_db.py`
> 把这段做成了自检，每次跑重新验一遍。

---

## 五、对照实验怎么设计的

三条链路喂同一份原始文档，区别只在数据怎么组织、怎么取：

| | 方案 | 说明 |
|---|---|---|
| ① | 传统 RAG | 切块 → 向量检索 top-k → 大模型读片段回答 |
| ② | 官方 naive Text2Cypher | 用 Neo4j 官方 `neo4j-graphrag` 包，几乎零代码 |
| ③ | 倒置LLM 增强版 | 本项目的实现：few-shot + 校验层 + 自修复 |

②和③的差距度量的是**工程化本身值多少钱**——同样的图、同样的模型，
把 prompt 工程和校验层加上去能提升多少。

还有一条补充基线：**把全部语料塞进上下文的「作弊版」传统 RAG**。
它能把两个失败原因分开：

- A 类问题上，作弊版会明显赢过 top-k 版 → 瓶颈是**检索**
- B 类问题上，连作弊版也答不对 → 瓶颈是**计算能力**，上下文再多也没用

**问题集分两层**，A 类 40–60 条、B 类 40–60 条：

| 层级 | 预期 | 作用 |
|---|---|---|
| A 类 | 传统 RAG 赢或打平 | 证明没作弊 |
| B 类 | 倒置LLM 显著赢 | 证明架构价值 |

**为什么不做「准确率提升 X%」这种结论.** 配对设计下，100–150 道题只能可靠检出
12–15 个百分点以上的差距。更小的差异测不出来，硬报一个数字是噪声。
所以最终报告只会给分层准确率加置信区间，并标注哪一层显著、哪一层看不出来。

评测的细节（指标怎么定、judge 怎么防偏、样本量怎么算）见 DESIGN.md 第六节。

---

## 六、跑起来

需要 Docker、Python 3.10+、[Ollama](https://ollama.com)。

```bash
# 起图数据库
docker compose up -d

# 装依赖
pip install -r requirements.txt
pip install -e .              # 别漏这步，否则 import app 会失败

# 配置
cp .env.example .env          # 按需改密码

# 自检：连通性、只读防线、schema
python scripts/init_db.py

# 准备模型
ollama pull qwen2.5-coder:14b
# 或者用本地 GGUF：
# ollama create qwen2.5-coder:14b -f models/Modelfile.qwen2.5-coder-14b
```

然后问一句：

```bash
python scripts/ask.py "3号泵停机会影响哪些设备？"
```

不加问题会进交互模式。主要参数：

| 参数 | 作用 |
|---|---|
| `--bench` | 跑内置的 12 条测试题 |
| `--no-db` | 只生成和校验，不连数据库 |
| `--show-prompt` | 打印实际发给模型的 prompt |
| `--no-examples` | 消融：关掉 few-shot |
| `--seed 42` | 固定随机种子 |

模型选型用 `python scripts/bench_models.py --models a b`。

**每次查询都会落一条 trace 到 `logs/query_trace.jsonl`**：模型原始输出、
每轮修复的失败原因、耗时拆解（加载 / prefill / 生成三段分开记）。
出问题翻记录就行，不用重跑。

---

## 七、现在做到哪了

### 能跑的

- Neo4j 5.26 社区版跑在 Docker 里，三层防线经实测有效
- 倒置LLM 主链路完整：生成 → 四道校验 → EXPLAIN → 只读执行 → 自修复
- 模型选型完成。`qwen2.5-coder:14b` 单条 0.6 秒，比初选的 27B 快 17 倍
- 32 个测试通过

### 还没做的

- **图数据库是空的。** `scripts/seed_graph.py` 还没写，现在问什么都返回 0 行。
  所以「答案对不对」暂时测不了，上面那些测的都是「结构对不对」
- 传统 RAG 基线和官方 naive 对照
- Web 界面
- 评测框架（问题集、judge、统计）
- 数据源等合作方提供

### 已知局限

**规模.** 图谱写的是 150–250 节点、30–50 页文档。调研显示约 56K token 以下的语料，
vanilla RAG 会打败所有图方法。我们用问题分层和「作弊版基线」把这个前提变成了
受控变量，但结论的适用范围仍然局限在这个量级，不能外推到几千篇文档的场景。

**校验层保证的是「能跑」.** 上面那三层能挡住不存在的标签、写反的方向、语法错误。
**挡不住语义错误。** 实测遇到过查询语法全对、能执行、返回非空结果，
但答非所问的情况。这类错误只能靠 few-shot 示例挡，见 DESIGN.md 第 3.11 节。

**「能执行率」是个陷阱指标.** 公开基准上 GPT-4o 的 Cypher 语法可执行率是 94.93%，
真实执行准确率 60.18%，相差约 35 个百分点。只报执行成功率的评测没有意义。

**没做的事.** 多源文档解析流水线、语音、AR、增量更新、线上部署。

---

## 八、文档与参考

| 文件 | 内容 |
|---|---|
| [DESIGN.md](DESIGN.md) | 技术设计。11 条核心决策，每条都写了代价；评测设计；调研依据 |
| [LOG.md](LOG.md) | 开发日志。按时间记做了什么、为什么、踩了什么坑 |

DESIGN.md 可以用 `python scripts/md2docx.py` 转成 Word，给不看 Markdown 的人用。

跑测试：

```bash
python -m pytest tests/ -v
```

`tests/test_client.py` 需要 Neo4j 在跑，不在会自动跳过。

设计时参考过的公开资料，按对本项目的影响排序：

- **CypherBench**（ACL 2025）—— Text2Cypher 的准确率天花板。GPT-4o 执行准确率
  60.18%，语法可执行率 94.93%，差 35 个百分点
- **Kuzu Text2Cypher 实验** —— schema 剪枝加方向提示后，30 条题 30/30 全对
- **GraphRAG-Bench**（ICLR 2026）—— 简单事实查询上 vanilla RAG 赢或打平，
  多跳和聚合上图方法赢。语料小于约 56K token 时 vanilla RAG 打败所有图方法
- **Text2TypeQL 语义错误分析** —— 方向错误 33%、计数错误 30%，
  幻觉 schema 元素只占 10%
- **SynthCypher**（ServiceNow）—— 代码专用模型微调后，7B 级别可接近 GPT-4o 水平

详细出处见 [DESIGN.md 第八节](DESIGN.md)。
