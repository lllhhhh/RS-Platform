"""
insar_client.py - InSAR 处理客户端

功能：
通过 HTTP API 调用 Docker 容器中的 GMTSAR 服务执行 InSAR 处理。

使用方式：
    from scripts.insar_client import process_insar, check_service_health

    # 检查服务是否可用
    if check_service_health():
        # 执行 InSAR 处理
        result = process_insar(
            task_id="test_001",
            master_path="tasks/xxx/downloads/s1_slc/S1A_...SAFE",
            slave_path="tasks/xxx/downloads/s1_slc/S1A_...SAFE",
            polarization="vv",
            subswath=2,
        )
"""

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# GMTSAR 服务地址
GMTSAR_SERVICE_URL = "http://localhost:8001"


def check_service_health() -> bool:
    """
    检查 GMTSAR 服务是否可用。

    Returns:
        bool: 服务是否健康且 GMTSAR 可用
    """
    try:
        url = f"{GMTSAR_SERVICE_URL}/health"
        response = requests.get(url, timeout=5)
        data = response.json()
        return (
            data.get("status") == "healthy"
            and data.get("gmtsar_available", False)
            and data.get("pygmtsar_available", False)
        )
    except Exception as e:
        print(f"[InSAR Client] 服务健康检查失败: {e}")
        return False


def process_insar(
    task_id: str,
    master_path: str,
    slave_path: str,
    polarization: str = "vv",
    subswath: int = 2,
    output_dir: str = None,
    timeout: int = 3600,
) -> dict:
    """
    调用 GMTSAR 服务执行 InSAR 处理。

    Args:
        task_id: 任务 ID
        master_path: 主影像路径（相对于 data 目录）
        slave_path: 从影像路径
        polarization: 极化通道（"vv" 或 "vh"）
        subswath: 子条带（1, 2, 3）
        output_dir: 输出目录（可选）
        timeout: 请求超时时间（秒，默认 1 小时）

    Returns:
        dict: 处理结果，包含以下字段：
            - status: "success" 或 "error"
            - task_id: 任务 ID
            - output_dir: 输出目录
            - files: 输出文件列表
            - report: 统计报告
            - message: 消息
    """
    url = f"{GMTSAR_SERVICE_URL}/insar/process"

    payload = {
        "task_id": task_id,
        "master_path": master_path,
        "slave_path": slave_path,
        "polarization": polarization,
        "subswath": subswath,
        "output_dir": output_dir,
    }

    print(f"[InSAR Client] 调用 GMTSAR 服务...")
    print(f"  任务 ID: {task_id}")
    print(f"  主影像: {master_path}")
    print(f"  从影像: {slave_path}")
    print(f"  极化: {polarization}, 子条带: {subswath}")

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()

        result = response.json()

        if result["status"] == "success":
            print(f"[InSAR Client] 处理成功")
            print(f"  输出目录: {result['output_dir']}")
            print(f"  输出文件: {list(result['files'].keys())}")
        else:
            print(f"[InSAR Client] 处理失败: {result['message']}")

        return result

    except requests.exceptions.Timeout:
        error_msg = f"请求超时（{timeout} 秒）"
        print(f"[InSAR Client] {error_msg}")
        return {
            "status": "error",
            "task_id": task_id,
            "output_dir": "",
            "files": {},
            "report": {},
            "message": error_msg,
        }

    except requests.exceptions.ConnectionError:
        error_msg = "无法连接到 GMTSAR 服务，请确保 Docker 容器已启动"
        print(f"[InSAR Client] {error_msg}")
        return {
            "status": "error",
            "task_id": task_id,
            "output_dir": "",
            "files": {},
            "report": {},
            "message": error_msg,
        }

    except Exception as e:
        error_msg = f"请求失败: {str(e)}"
        print(f"[InSAR Client] {error_msg}")
        return {
            "status": "error",
            "task_id": task_id,
            "output_dir": "",
            "files": {},
            "report": {},
            "message": error_msg,
        }


def main():
    """独立运行：测试 GMTSAR 服务连接。"""
    import argparse

    global GMTSAR_SERVICE_URL

    parser = argparse.ArgumentParser(description="InSAR 客户端测试工具")
    parser.add_argument("--check", action="store_true", help="检查服务健康状态")
    parser.add_argument("--url", type=str, default=GMTSAR_SERVICE_URL, help="服务 URL")

    args = parser.parse_args()

    GMTSAR_SERVICE_URL = args.url

    print("=" * 60)
    print("InSAR 客户端测试")
    print("=" * 60)
    print(f"服务地址: {GMTSAR_SERVICE_URL}")
    print()

    if args.check or True:
        print("检查服务健康状态...")
        healthy = check_service_health()

        if healthy:
            print("OK: 服务健康，GMTSAR 可用")
        else:
            print("ERROR: 服务不可用或 GMTSAR 未就绪")
            print()
            print("请确保 Docker 容器已启动：")
            print("  docker-compose up -d gmtsar")
            print()
            print("检查容器状态：")
            print("  docker-compose ps")
            print("  docker-compose logs gmtsar")


if __name__ == "__main__":
    main()
