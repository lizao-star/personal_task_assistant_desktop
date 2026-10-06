"""一次性连接测试脚本：验证飞书凭证、权限与表格读取（不启动 GUI）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_config
from app.feishu.client import FeishuClient
from app.feishu.repository import list_tasks


def main() -> int:
    config = load_config()
    c = config.credential
    if not c.is_ready:
        print("[FAIL] 凭证未配置完整，请检查 config/.env")
        return 1

    print(f"[1/3] 凭证读取成功  app_id={c.app_id[:8]}***")
    client = FeishuClient(c.app_id, c.app_secret, timeout=10)
    token = client.get_token()
    print(f"[2/3] tenant_access_token 获取成功  {token[:12]}***")

    tasks = list_tasks(client, c.app_token, c.task_table_id)
    print(f"[3/3] 任务表读取成功，共 {len(tasks)} 条记录")
    for t in tasks[:5]:
        print(f"      - {t.title} | 状态={t.status} | 优先级={t.priority} | 截止={t.due_at}")
    if len(tasks) > 5:
        print(f"      ... 其余 {len(tasks) - 5} 条省略")
    print("\n全部通过！可以直接运行 python run.py 启动客户端。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
