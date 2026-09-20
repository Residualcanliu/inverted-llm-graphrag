"""初始化 / 自检 Neo4j 连接。

不建账号 —— 社区版没有 RBAC，买了也用不上（见 app/graph/client.py 文件头）。
改做一件更有用的事：验证只读防线真的生效。

幂等，可反复跑。用法：python scripts/init_db.py
"""

from __future__ import annotations

import sys

# Windows 控制台默认 GBK，中文会变乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                     # noqa: BLE001
    pass

from neo4j import READ_ACCESS                    # noqa: E402

from app import config                           # noqa: E402
from app.graph import client                     # noqa: E402

# 每种写操作都要拦下。只测 CREATE 不够 —— 换一种写法绕过就白搭了。
WRITE_PROBES = [
    ("CREATE", "CREATE (x:__Probe__) RETURN x"),
    ("MERGE", "MERGE (x:__Probe__ {id:'p'}) RETURN x"),
    ("SET", "MATCH (n) SET n.__probe__ = 1 RETURN n"),
    ("DELETE", "MATCH (n:__Probe__) DELETE n"),
    ("DETACH DELETE", "MATCH (n) DETACH DELETE n"),
    ("REMOVE", "MATCH (n) REMOVE n.__probe__ RETURN n"),
    ("APOC 写过程", "CALL apoc.create.node(['__Probe__'], {}) YIELD node RETURN node"),
]


def check_readonly_enforced() -> tuple[bool, list[str]]:
    """尝试各种写操作，全部必须被服务端拒绝。"""
    detail = []
    all_ok = True
    with client.get_driver() as d:
        for name, q in WRITE_PROBES:
            try:
                with d.session(default_access_mode=READ_ACCESS) as s:
                    s.run(q).consume()
                detail.append(f"漏过 {name}")
                all_ok = False
            except Exception as e:                    # noqa: BLE001
                code = getattr(e, "code", "") or type(e).__name__
                if "AccessMode" in str(code):
                    detail.append(f"{name} 被拒")
                else:
                    # 报的不是权限错，说明这条探针本身写得有问题，也要报出来
                    detail.append(f"{name} 报了非预期的错：{str(code)[:40]}")
                    all_ok = False
        # 对照：只读查询必须能通过
        try:
            with d.session(default_access_mode=READ_ACCESS) as s:
                s.run("MATCH (n) RETURN count(n) AS c").consume()
            detail.append("只读查询通过")
        except Exception as e:                        # noqa: BLE001
            detail.append(f"只读查询失败：{e}")
            all_ok = False
    return all_ok, detail


def main() -> int:
    print("=" * 64)
    print("Neo4j 检查")
    print("=" * 64)
    print(f"  地址 {config.NEO4J_URI}   账号 {config.NEO4J_USER}")
    print()

    ok, msg = client.ping()
    print(f"[{'OK' if ok else 'FAIL'}] 连通性：{msg}")
    if not ok:
        print("\n连不上时依次检查：")
        print("  1. 容器在跑吗    docker compose ps")
        print("  2. 密码对得上吗  .env 的 NEO4J_PASSWORD 要和 docker-compose.yml 一致")
        print("  3. 端口通吗      curl http://localhost:7474")
        return 1

    try:
        with client.get_driver() as d, d.session() as s:
            edition = s.run(
                "CALL dbms.components() YIELD edition RETURN edition"
            ).single()["edition"]
        print(f"[OK]   版本：Neo4j {edition} 版".replace("OK]   ", "OK] "))
    except Exception:                                 # noqa: BLE001
        pass

    ok, detail = check_readonly_enforced()
    print(f"[{'OK' if ok else 'FAIL'}] 只读防线：{'，'.join(detail)}")
    if not ok:
        print("       这一项失败意味着写操作能穿过去，必须修完再往下走")
        return 1

    print()
    try:
        sc = client.fetch_schema()
        n_labels, n_rels = len(sc["nodes"]), len(sc["rels"])
        print(f"  图谱现状：{n_labels} 种节点标签，{n_rels} 种关系类型")
        if n_labels == 0:
            print("  空库。下一步：python scripts/seed_graph.py")
        else:
            for lb, props in sorted(sc["nodes"].items()):
                print(f"    {lb}: {', '.join(props) or '无属性'}")
    except Exception as e:                            # noqa: BLE001
        print(f"  读取 schema 失败：{e}")

    print()
    print("  Neo4j Browser: http://localhost:7474")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
