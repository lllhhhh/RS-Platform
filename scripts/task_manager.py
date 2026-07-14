"""
task_manager.py - 任务管理工具

功能：
1. 创建独立的任务目录
2. 列出所有任务
3. 查看任务详情
4. 清理旧任务
5. 管理 task_latest 软链接

使用方法：
    python scripts/task_manager.py list
    python scripts/task_manager.py info 20240115_120000_S1_SLC
    python scripts/task_manager.py cleanup --keep 5
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DATA_DIR


def get_tasks_dir() -> Path:
    """获取任务目录路径。"""
    return DATA_DIR / "tasks"


def generate_task_id(satellite: str = "sentinel2", s1_product: str = "grd") -> str:
    """
    生成任务ID。

    格式: {时间戳}_{卫星类型}
    示例: 20240115_120000_S1_SLC, 20240115_120000_S2

    Args:
        satellite: 卫星类型
        s1_product: S1产品类型

    Returns:
        str: 任务ID
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if satellite == "sentinel1":
        sat_label = f"S1_{s1_product.upper()}"
    else:
        sat_label = "S2"

    return f"{timestamp}_{sat_label}"


def create_task_dir(task_id: str = None, satellite: str = "sentinel2", s1_product: str = "grd") -> Path:
    """
    创建任务目录。

    Args:
        task_id: 任务ID（为None时自动生成）
        satellite: 卫星类型
        s1_product: S1产品类型

    Returns:
        Path: 任务目录路径
    """
    if task_id is None:
        task_id = generate_task_id(satellite, s1_product)

    tasks_dir = get_tasks_dir()
    task_dir = tasks_dir / task_id

    # 创建子目录
    subdirs = ["downloads", "merged", "cloud_masked", "mosaicked", "zarr", "insar"]
    for subdir in subdirs:
        (task_dir / subdir).mkdir(parents=True, exist_ok=True)

    # 创建任务信息文件
    task_info = {
        "task_id": task_id,
        "created_at": datetime.now().isoformat(),
        "satellite": satellite,
        "s1_product": s1_product,
        "status": "created",
    }

    with open(task_dir / "task_info.json", "w", encoding="utf-8") as f:
        json.dump(task_info, f, ensure_ascii=False, indent=2)

    # 更新 task_latest 软链接
    update_latest_link(task_id)

    return task_dir


def update_latest_link(task_id: str) -> None:
    """
    更新 task_latest 软链接。

    Args:
        task_id: 任务ID
    """
    tasks_dir = get_tasks_dir()
    latest_link = tasks_dir / "task_latest"
    target = tasks_dir / task_id

    # 删除旧链接
    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()

    # 创建新链接（Windows 使用目录连接）
    try:
        latest_link.symlink_to(target, target_is_directory=True)
    except OSError:
        # Windows 上可能需要管理员权限，改用复制方式
        # 这里只是记录最新任务ID
        latest_info = {"latest_task_id": task_id}
        with open(tasks_dir / "latest_task.json", "w", encoding="utf-8") as f:
            json.dump(latest_info, f)


def get_latest_task_id() -> str:
    """
    获取最新任务ID。

    Returns:
        str: 最新任务ID，如果没有任务则返回None
    """
    tasks_dir = get_tasks_dir()

    # 尝试从软链接获取
    latest_link = tasks_dir / "task_latest"
    if latest_link.exists() and latest_link.is_symlink():
        return latest_link.resolve().name

    # 尝试从 latest_task.json 获取
    latest_file = tasks_dir / "latest_task.json"
    if latest_file.exists():
        with open(latest_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("latest_task_id")

    # 返回最新的任务目录
    tasks = list_tasks()
    if tasks:
        return tasks[0]["task_id"]

    return None


def get_task_dir(task_id: str = None) -> Path:
    """
    获取任务目录路径。

    Args:
        task_id: 任务ID（为None时使用最新任务）

    Returns:
        Path: 任务目录路径
    """
    if task_id is None:
        task_id = get_latest_task_id()

    if task_id is None:
        return None

    return get_tasks_dir() / task_id


def list_tasks() -> list:
    """
    列出所有任务。

    Returns:
        list: 任务信息列表（按创建时间倒序）
    """
    tasks_dir = get_tasks_dir()
    if not tasks_dir.exists():
        return []

    tasks = []
    for task_dir in sorted(tasks_dir.iterdir(), reverse=True):
        if not task_dir.is_dir() or task_dir.name.startswith("task_"):
            continue

        task_info_file = task_dir / "task_info.json"
        if task_info_file.exists():
            with open(task_info_file, "r", encoding="utf-8") as f:
                task_info = json.load(f)
                task_info["path"] = str(task_dir)
                tasks.append(task_info)
        else:
            # 兼容旧目录
            tasks.append({
                "task_id": task_dir.name,
                "path": str(task_dir),
                "status": "unknown",
            })

    return tasks


def get_task_info(task_id: str) -> dict:
    """
    获取任务详情。

    Args:
        task_id: 任务ID

    Returns:
        dict: 任务信息
    """
    task_dir = get_tasks_dir() / task_id
    if not task_dir.exists():
        return None

    task_info_file = task_dir / "task_info.json"
    if task_info_file.exists():
        with open(task_info_file, "r", encoding="utf-8") as f:
            task_info = json.load(f)
    else:
        task_info = {"task_id": task_id}

    # 添加目录信息
    task_info["path"] = str(task_dir)

    # 统计文件
    file_stats = {}
    for subdir in ["downloads", "merged", "cloud_masked", "mosaicked", "zarr", "insar"]:
        subdir_path = task_dir / subdir
        if subdir_path.exists():
            files = list(subdir_path.glob("*"))
            file_stats[subdir] = len(files)
    task_info["file_stats"] = file_stats

    return task_info


def update_task_info(task_id: str, updates: dict) -> None:
    """
    更新任务信息。

    Args:
        task_id: 任务ID
        updates: 要更新的字段
    """
    task_dir = get_tasks_dir() / task_id
    task_info_file = task_dir / "task_info.json"

    if task_info_file.exists():
        with open(task_info_file, "r", encoding="utf-8") as f:
            task_info = json.load(f)
    else:
        task_info = {"task_id": task_id}

    task_info.update(updates)
    task_info["updated_at"] = datetime.now().isoformat()

    with open(task_info_file, "w", encoding="utf-8") as f:
        json.dump(task_info, f, ensure_ascii=False, indent=2)


def cleanup_tasks(keep_count: int = 5) -> list:
    """
    清理旧任务，保留最近的N个。

    Args:
        keep_count: 保留的任务数量

    Returns:
        list: 被删除的任务ID列表
    """
    tasks = list_tasks()

    if len(tasks) <= keep_count:
        return []

    to_delete = tasks[keep_count:]
    deleted = []

    for task in to_delete:
        task_id = task["task_id"]
        task_dir = Path(task["path"])

        if task_dir.exists():
            shutil.rmtree(task_dir)
            deleted.append(task_id)

    return deleted


# ============================================================
# CLI 命令
# ============================================================

def cmd_list(args):
    """列出所有任务。"""
    tasks = list_tasks()

    if not tasks:
        print("没有找到任务")
        return

    print("\n" + "=" * 80)
    print("任务列表")
    print("=" * 80)
    print(f"{'任务ID':<35} {'卫星':<10} {'状态':<12} {'创建时间'}")
    print("-" * 80)

    for task in tasks:
        task_id = task.get("task_id", "unknown")
        satellite = task.get("satellite", "-")
        status = task.get("status", "-")
        created = task.get("created_at", "-")[:19]

        print(f"{task_id:<35} {satellite:<10} {status:<12} {created}")

    print("\n" + f"共 {len(tasks)} 个任务")


def cmd_info(args):
    """查看任务详情。"""
    task_info = get_task_info(args.task_id)

    if task_info is None:
        print(f"任务不存在: {args.task_id}")
        return

    print("\n" + "=" * 60)
    print("任务详情")
    print("=" * 60)

    for key, value in task_info.items():
        if key == "file_stats":
            print(f"\n文件统计:")
            for subdir, count in value.items():
                print(f"  {subdir}: {count} 个文件")
        else:
            print(f"{key}: {value}")


def cmd_cleanup(args):
    """清理旧任务。"""
    tasks = list_tasks()

    if len(tasks) <= args.keep:
        print(f"当前有 {len(tasks)} 个任务，无需清理（保留 {args.keep} 个）")
        return

    print(f"将删除 {len(tasks) - args.keep} 个旧任务（保留最近 {args.keep} 个）")

    if not args.yes:
        confirm = input("确认删除？(y/N): ").strip().lower()
        if confirm != 'y':
            print("已取消")
            return

    deleted = cleanup_tasks(args.keep)

    if deleted:
        print(f"已删除 {len(deleted)} 个任务:")
        for task_id in deleted:
            print(f"  - {task_id}")
    else:
        print("没有任务被删除")


def main():
    parser = argparse.ArgumentParser(description="任务管理工具")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # list 命令
    subparsers.add_parser("list", help="列出所有任务")

    # info 命令
    info_parser = subparsers.add_parser("info", help="查看任务详情")
    info_parser.add_argument("task_id", help="任务ID")

    # cleanup 命令
    cleanup_parser = subparsers.add_parser("cleanup", help="清理旧任务")
    cleanup_parser.add_argument("--keep", type=int, default=5, help="保留的任务数量（默认: 5）")
    cleanup_parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "info":
        cmd_info(args)
    elif args.command == "cleanup":
        cmd_cleanup(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
