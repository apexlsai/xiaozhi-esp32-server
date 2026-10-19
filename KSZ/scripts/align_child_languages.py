import argparse
import json
import os
from pathlib import Path
import subprocess


REPLACEMENTS = {
    "儿童-英语": ("You primary reply in Chinese", "You primarily reply in English"),
    "儿童-日语": ("主に中国語で回答し", "主に日本語で回答し"),
    "儿童-韩语": ("주로 중국어로 답변하며", "주로 한국어로 답변하며"),
}


def sql_text(value):
    return "CONVERT(0x" + value.encode("utf-8").hex() + " USING utf8mb4)"


class Database:
    def __init__(self, container):
        self.process = subprocess.Popen(
            ["docker", "exec", "-i", container, "sh", "-c",
             'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --batch --raw '
             '--skip-column-names --unbuffered --default-character-set=utf8mb4 xiaozhi_esp32_server'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

    def query(self, sql):
        self.process.stdin.write(sql + ";\nSELECT '__KSZ_DONE__';\n")
        self.process.stdin.flush()
        rows = []
        for line in self.process.stdout:
            value = line.rstrip("\n")
            if value == "__KSZ_DONE__":
                return rows
            rows.append(value)
        raise RuntimeError("数据库命令失败，事务已终止")

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="xiaozhi-esp32-server-db")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--rollback", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and not (args.backup or args.rollback):
        parser.error("--apply 需要 --backup 或 --rollback")
    database = Database(args.container)
    try:
        database.query("START TRANSACTION")
        if args.rollback:
            changes = json.loads(args.rollback.read_text())
            for change in changes:
                change["before"], change["after"] = change["after"], change["before"]
        else:
            names = ",".join(sql_text(name) for name in REPLACEMENTS)
            rows = database.query(
                "SELECT JSON_OBJECT('id',id,'name',agent_name,'before',system_prompt) "
                f"FROM ai_agent WHERE agent_name IN ({names}) ORDER BY id FOR UPDATE"
            )
            changes = [json.loads(row) for row in rows]
            if len(changes) != 3 or {row["name"] for row in changes} != set(REPLACEMENTS):
                raise RuntimeError("目标角色不唯一或缺失，未修改")
            for change in changes:
                old, new = REPLACEMENTS[change["name"]]
                prompt = change["before"]
                if prompt.count(old) == 0 and new in prompt:
                    change["after"] = prompt
                elif prompt.count(old) == 1:
                    change["after"] = prompt.replace(old, new, 1)
                else:
                    raise RuntimeError(f"{change['name']} 的原指令已变化，未修改")
        changes = [change for change in changes if change["before"] != change["after"]]
        if args.apply and args.backup and not args.rollback:
            with open(args.backup, "x", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                json.dump(changes, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        if args.apply:
            for change in changes:
                rows = database.query(
                    f"UPDATE ai_agent SET system_prompt={sql_text(change['after'])},updated_at=NOW() "
                    f"WHERE id={sql_text(change['id'])} AND BINARY system_prompt=BINARY {sql_text(change['before'])};"
                    "SELECT ROW_COUNT()"
                )
                if rows != ["1"]:
                    raise RuntimeError("角色已被并发修改，整批事务回滚")
            database.query("COMMIT")
        else:
            database.query("ROLLBACK")
        print(json.dumps({"applied": args.apply, "roles": [row["name"] for row in changes]}, ensure_ascii=False))
    except BaseException:
        if database.process.poll() is None:
            database.query("ROLLBACK")
        raise
    finally:
        database.close()


if __name__ == "__main__":
    main()
