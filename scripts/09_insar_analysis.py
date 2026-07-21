"""
09_insar_analysis.py - InSAR 形变监测模块

功能：
通过 GMTSAR Docker 服务对两幅 Sentinel-1 SLC 影像执行 InSAR 处理流程，
生成形变图、相干性图和形变分析报告。

InSAR 处理链（在 Docker 容器中执行）：
1. 扫描 SLC 场景
2. 下载轨道文件
3. 下载 DEM
4. 配准（Coregistration）
5. 干涉图生成（含 DEM 去地形相位）
6. Goldstein 滤波
7. 相位解缠（Snaphu）
8. 形变提取
9. 导出 GeoTIFF

使用方法：
    # 方式 1：使用 pipeline 下载的数据（从任务目录）
    python scripts/09_insar_analysis.py --data-dir ./data/tasks/20240115_120000_S1_SLC

    # 方式 2：指定 SLC 数据目录
    python scripts/09_insar_analysis.py --slc-dir ./data/tasks/xxx/downloads/s1_slc

    # 方式 3：直接指定主从影像
    python scripts/09_insar_analysis.py --master path/master.SAFE --slave path/slave.SAFE

    # 方式 4：交互式选择（从默认 data 目录）
    python scripts/09_insar_analysis.py --data-dir ./data
"""

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def list_slc_scenes(data_dir: Path, slc_dir: Path = None) -> list:
    """
    列出可用的 SLC 影像。

    查找顺序：
    1. 如果指定了 slc_dir，直接从该目录查找
    2. 从 data_dir/downloads/s1_slc 查找 SAFE 目录
    3. 从 data_dir/merged 查找合成 TIF
    4. 从 data_dir/mosaicked 查找裁剪 TIF

    Args:
        data_dir: 数据目录（包含 downloads/s1_slc 子目录）
        slc_dir: 直接指定的 SLC 目录（优先使用）

    Returns:
        list: SLC 影像路径列表
    """
    # 如果直接指定了 SLC 目录，优先使用
    if slc_dir and slc_dir.exists():
        # 查找 SAFE 目录（InSAR 需要完整 SAR 元数据）
        safe_dirs = sorted(slc_dir.glob("*.SAFE"))
        if safe_dirs:
            return safe_dirs
        # 也支持 TIF 文件
        tif_files = sorted(slc_dir.glob("*.tif"))
        if tif_files:
            return tif_files
        # 递归查找子目录中的 SAFE
        safe_dirs = sorted(slc_dir.glob("**/*.SAFE"))
        if safe_dirs:
            return safe_dirs

    # 从 data_dir/downloads/s1_slc 查找
    s1_slc_dir = data_dir / "downloads" / "s1_slc"
    if s1_slc_dir.exists():
        safe_dirs = sorted(s1_slc_dir.glob("*.SAFE"))
        if safe_dirs:
            return safe_dirs

    # 从 data_dir/merged 查找 SLC 合成影像
    merged_dir = data_dir / "merged"
    if merged_dir.exists():
        slc_files = sorted(merged_dir.glob("*_S1_merged.tif"))
        if slc_files:
            return slc_files

    # 从 data_dir/mosaicked 查找裁剪影像
    mosaicked_dir = data_dir / "mosaicked"
    if mosaicked_dir.exists():
        slc_files = sorted(mosaicked_dir.glob("*_clipped.tif"))
        if slc_files:
            return slc_files

    return []


def _get_safe_display_name(safe_path: Path) -> str:
    """从 SAFE 路径提取简洁的显示名称。"""
    name = safe_path.name
    if name.endswith(".SAFE"):
        name = name[:-5]
    return name


def select_scenes_interactive(slc_files: list) -> tuple:
    """
    交互式选择主影像和从影像。

    Args:
        slc_files: 可用 SLC 影像路径列表

    Returns:
        tuple: (master_path, slave_path)
    """
    print("\n" + "=" * 60)
    print("可用 SLC 影像列表")
    print("=" * 60)

    for i, f in enumerate(slc_files):
        print(f"  {i + 1}. {f.name if f.suffix == '.tif' else _get_safe_display_name(f)}")

    print("-" * 60)
    print("请选择两幅影像进行 InSAR 分析（先选主影像，再选从影像）")
    print("提示: 主影像应为时间较早的影像")

    while True:
        try:
            master_input = input("\n请输入主影像序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return None, None

        try:
            master_idx = int(master_input) - 1
            if 0 <= master_idx < len(slc_files):
                break
            print(f"序号超出范围，请输入 1~{len(slc_files)}")
        except ValueError:
            print("请输入数字序号")

    while True:
        try:
            slave_input = input("请输入从影像序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return None, None

        try:
            slave_idx = int(slave_input) - 1
            if 0 <= slave_idx < len(slc_files):
                if slave_idx == master_idx:
                    print("主从影像不能相同，请重新选择")
                    continue
                break
            print(f"序号超出范围，请输入 1~{len(slc_files)}")
        except ValueError:
            print("请输入数字序号")

    master_path = slc_files[master_idx]
    slave_path = slc_files[slave_idx]

    print(f"\n主影像: {master_path.name}")
    print(f"从影像: {slave_path.name}")

    return master_path, slave_path


def run_insar(
    master_path: Path,
    slave_path: Path,
    polarization: str = "vv",
    output_dir: Path = None,
) -> dict:
    """
    执行 InSAR 处理流程。

    通过 GMTSAR Docker 服务执行完整的 InSAR 处理链。

    Args:
        master_path: 主影像路径
        slave_path: 从影像路径
        polarization: 极化通道（"vv" 或 "vh"）
        output_dir: 输出目录

    Returns:
        dict: 处理结果信息
    """
    from scripts.insar_client import check_service_health, process_insar

    # 检查服务可用性
    print("[InSAR] 检查 GMTSAR 服务...")
    if not check_service_health():
        print("[错误] GMTSAR 服务不可用")
        print("  请确保 Docker 容器已启动：")
        print("    docker-compose up -d gmtsar")
        print()
        print("  检查容器状态：")
        print("    docker-compose ps")
        print("    docker-compose logs gmtsar")
        sys.exit(1)

    print("[InSAR] GMTSAR 服务就绪")

    # 生成任务 ID
    task_id = f"insar_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 计算相对路径
    data_dir = Path(__file__).resolve().parent.parent / "data"

    # 尝试计算相对路径
    try:
        master_rel = str(master_path.relative_to(data_dir))
        slave_rel = str(slave_path.relative_to(data_dir))
    except ValueError:
        # 如果路径不在 data_dir 下，使用绝对路径
        master_rel = str(master_path)
        slave_rel = str(slave_path)

    # 确定输出目录
    if output_dir is None:
        output_dir = data_dir / "tasks" / task_id / "insar"

    output_rel = None
    try:
        output_rel = str(output_dir.relative_to(data_dir))
    except ValueError:
        pass

    # 调用服务
    result = process_insar(
        task_id=task_id,
        master_path=master_rel,
        slave_path=slave_rel,
        polarization=polarization,
        subswath=2,  # 默认使用 IW2
        output_dir=output_rel,
    )

    if result["status"] == "error":
        raise RuntimeError(f"InSAR 处理失败: {result['message']}")

    # 转换为本地路径
    result["output_dir"] = str(data_dir / result["output_dir"])
    result["files"] = {k: str(data_dir / v) for k, v in result["files"].items()}

    return result


def main():
    """主函数：解析参数并执行 InSAR 分析。"""
    parser = argparse.ArgumentParser(
        description="InSAR 形变监测工具（基于 GMTSAR Docker 服务）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例：
  # 方式 1：使用 pipeline 下载的数据（从任务目录）
  python scripts/09_insar_analysis.py --data-dir ./data/tasks/20240115_120000_S1_SLC

  # 方式 2：指定 SLC 数据目录
  python scripts/09_insar_analysis.py --slc-dir ./data/tasks/xxx/downloads/s1_slc

  # 方式 3：直接指定主从影像
  python scripts/09_insar_analysis.py --master path/master.SAFE --slave path/slave.SAFE

  # 方式 4：交互式选择（从默认 data 目录）
  python scripts/09_insar_analysis.py --data-dir ./data
        """,
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="数据目录（包含 downloads/s1_slc 子目录）",
    )
    parser.add_argument(
        "--slc-dir",
        type=str,
        default=None,
        help="SLC 数据目录（直接指定，优先于 --data-dir）",
    )
    parser.add_argument(
        "--master",
        type=str,
        default=None,
        help="主影像路径（不提供则交互选择）",
    )
    parser.add_argument(
        "--slave",
        type=str,
        default=None,
        help="从影像路径（不提供则交互选择）",
    )
    parser.add_argument(
        "--polarization",
        type=str,
        default="vv",
        choices=["vv", "vh"],
        help="极化通道（默认 vv）",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="输出目录（默认 data/tasks/TASK_ID/insar）",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    slc_dir = Path(args.slc_dir) if args.slc_dir else None
    output_dir = Path(args.output_dir) if args.output_dir else None

    print("=" * 60)
    print("RS-Platform: InSAR 形变监测")
    print("=" * 60)
    print("使用 GMTSAR Docker 服务进行处理")
    if slc_dir:
        print(f"SLC 数据目录: {slc_dir}")
    else:
        print(f"数据目录: {data_dir}")
    print()

    # 确定主从影像
    if args.master and args.slave:
        master_path = Path(args.master)
        slave_path = Path(args.slave)
    else:
        slc_files = list_slc_scenes(data_dir, slc_dir)
        if len(slc_files) < 2:
            print(f"[错误] 需要至少 2 幅 SLC 影像，当前找到 {len(slc_files)} 幅")
            if slc_dir:
                print(f"  请检查 SLC 目录: {slc_dir}")
            else:
                print(f"  请先通过管线下载 SLC 影像：")
                print(f"  python scripts/06_pipeline.py --satellite sentinel1 --s1-product slc --bbox ...")
                print(f"  或使用 --slc-dir 参数指定 SLC 数据目录")
            return

        print(f"找到 {len(slc_files)} 幅 SLC 影像")
        master_path, slave_path = select_scenes_interactive(slc_files)
        if master_path is None:
            return

    # 执行 InSAR 处理
    try:
        result = run_insar(
            master_path=master_path,
            slave_path=slave_path,
            polarization=args.polarization,
            output_dir=output_dir,
        )

        print(f"\n{'=' * 60}")
        print(f"InSAR 处理完成！")
        print(f"{'=' * 60}")
        print(f"输出目录: {result['output_dir']}")
        print(f"输出文件:")
        for name, path in result["files"].items():
            print(f"  {name}: {Path(path).name}")

        if result.get("report", {}).get("deformation"):
            stats = result["report"]["deformation"]
            print(f"\n形变统计:")
            print(f"  均值: {stats.get('mean_mm', 0):.2f} mm")
            print(f"  标准差: {stats.get('std_mm', 0):.2f} mm")
            print(f"  最大抬升: {stats.get('max_uplift_mm', 0):.2f} mm")
            print(f"  最大沉降: {stats.get('max_subsidence_mm', 0):.2f} mm")

    except Exception as e:
        print(f"\n[错误] InSAR 处理失败: {e}")
        traceback.print_exc()
        return


if __name__ == "__main__":
    main()
