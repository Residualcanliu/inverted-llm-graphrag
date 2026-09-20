"""改 Neo4j 密码。做三件事：改库里的、改 .env 里的、验证两边都对上了。

**为什么不能用改配置文件的方式**

`docker-compose.yml` 里的 `NEO4J_AUTH` 只在数据库**首次初始化**时生效 ——
容器第一次启动、数据目录还是空的时候，它把密码写进认证存储，之后就不管了。

实测（Neo4j 5.26 社区版）：

    改 NEO4J_AUTH → docker compose up -d --force-recreate → 重启完成
    旧密码  -> 仍然能连
    新密码  -> AuthError

所以在已有数据的情况下，改配置文件是无效操作，只会让人以为改了。

**两条正确的路**

1. 库里改（本脚本走这条）。库和密码都保留，适合有数据的时候。
2. 推倒重来。删掉数据卷让 NEO4J_AUTH 重新初始化：
       docker compose down -v      # 注意 -v 会删掉所有图数据
       docker compose up -d
   库是空的时候这条最省事；有数据就没了。

用法：
    python scripts/set_password.py 新密码
    python scripts/set_password.py 新密码 --print-only   # 只提示不执行
"""

from __future__ import annotations

import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                     # noqa: BLE001
    pass

from neo4j import GraphDatabase                       # noqa: E402

from app import config                                # noqa: E402

ENV_FILE = config.ROOT / ".env"


def update_env(new_password: str) -> bool:
    """把新密码写进 .env。保留其余行和注释。"""
    if not ENV_FILE.exists():
        print(f"  [警告] 找不到 {ENV_FILE.name}，请手工把密码改成新值")
        return False
    text = ENV_FILE.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r"^NEO4J_PASSWORD=.*$",
        f"NEO4J_PASSWORD={new_password}",
        text,
        flags=re.MULTILINE,
    )
    if n == 0:
        new_text = text.rstrip("\n") + f"\nNEO4J_PASSWORD={new_password}\n"
    ENV_FILE.write_text(new_text, encoding="utf-8")
    print(f"  [OK]   已更新 {ENV_FILE.name}")
    return True


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    print_only = "--print-only" in sys.argv

    if not args:
        print(__doc__)
        print("用法：python scripts/set_password.py 新密码")
        return 1

    new_password = args[0]
    if len(new_password) < 8:
        # Neo4j 默认密码策略要求至少 8 位
        print("密码太短。Neo4j 默认策略要求至少 8 个字符。")
        return 1

    old_password = config.NEO4J_PASSWORD
    print(f"地址     {config.NEO4J_URI}")
    print(f"账号     {config.NEO4J_USER}")
    print(f"当前密码 {'*' * len(old_password)}")
    print(f"新密码   {'*' * len(new_password)}")
    print()

    if print_only:
        print("--print-only，未执行。")
        return 0

    # 1. 用旧密码连上，改库里的
    try:
        with GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, old_password),
        ) as d, d.session() as s:
            s.run("ALTER CURRENT USER SET PASSWORD FROM $old TO $new",
                  old=old_password, new=new_password).consume()
        print("  [OK]   库里的密码已改")
    except Exception as e:                            # noqa: BLE001
        msg = str(e)
        print(f"  [FAIL] 改库密码失败：{msg[:160]}")
        if "AuthenticationRateLimit" in msg or "authentication failure" in msg.lower():
            print()
            print("  这通常意味着 .env 里的密码和库里的不一致。两个办法：")
            print("    a) 把 .env 的 NEO4J_PASSWORD 改回库里的旧密码，再跑一次")
            print("    b) 库是空的话，直接推倒重来：")
            print("         docker compose down -v && docker compose up -d")
        return 1

    # 2. 同步 .env
    update_env(new_password)

    # 3. 验证：新密码能连，旧密码连不上
    print()
    ok_new = ok_old = False
    for pw, label in ((new_password, "新密码"), (old_password, "旧密码")):
        try:
            with GraphDatabase.driver(config.NEO4J_URI,
                                      auth=(config.NEO4J_USER, pw)) as d:
                d.verify_connectivity()
            print(f"  {label} -> 可以连接")
            if label == "新密码":
                ok_new = True
            else:
                ok_old = True
        except Exception:                             # noqa: BLE001
            print(f"  {label} -> 已失效")

    print()
    if ok_new and not ok_old:
        print("改好了。注意 config.py 会在下次进程启动时读到新值。")
        return 0
    print("[警告] 验证结果不符合预期，请手工确认。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
