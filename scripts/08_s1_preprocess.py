"""
08_s1_preprocess.py - Sentinel-1 GRD 预处理（纯 Python 实现）

功能：
对 Sentinel-1 GRD 影像执行标准预处理流程：
1. 读取 GRD 数据（通过 xarray-sentinel）
2. 应用轨道文件（自动下载）
3. 地形校正（几何校正）
4. 辐射校正（gamma flattening）
5. 转化为分贝值

依赖：
- sarsen（纯 Python SAR 处理库）
- xarray-sentinel（Sentinel-1 数据读取）
- pygmtsar（DEM 下载，可选）

处理输入：merged/ 目录下的 S1 多通道 TIF（VV+VH）
处理输出：cloud_masked/ 目录下的预处理后 TIF（复用后续管线流程）

使用方法：
    python scripts/08_s1_preprocess.py --data-dir ./data
    python scripts/08_s1_preprocess.py --data-dir ./data --scene-ids S1A_IW_GRDH_...
"""

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def preprocess_s1_scenes(data_dir: Path, scene_ids: set = None) -> list:
    """
    对所有 S1 GRD 合成后的 TIF 执行预处理。

    处理流程：
    1. 扫描 merged/ 目录下 *_S1_merged.tif
    2. 对每个文件执行 sarsen 预处理
    3. 输出到 cloud_masked/ 目录（复用后续管线流程）

    Args:
        data_dir: 数据目录
        scene_ids: 要处理的 scene_id 集合，为 None 时处理全部

    Returns:
        list: 预处理后的 TIF 文件路径列表
    """
    from scripts.s1_preprocess_python import preprocess_s1_grd_scenes

    # 调用纯 Python 实现
    return preprocess_s1_grd_scenes(data_dir, scene_ids=scene_ids)


def main():
    """主函数：解析参数并执行 S1 预处理。"""
    parser = argparse.ArgumentParser(
        description="Sentinel-1 GRD 预处理工具（纯 Python，基于 sarsen）",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="数据目录（包含 merged 子目录）",
    )
    parser.add_argument(
        "--scene-ids",
        type=str,
        nargs="+",
        default=None,
        help="要处理的 scene ID 列表（可选）",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    scene_ids = set(args.scene_ids) if args.scene_ids else None

    print("=" * 60)
    print("RS-Platform: Sentinel-1 GRD 预处理")
    print("=" * 60)
    print("使用 sarsen 库进行地形校正和辐射校正（纯 Python）")
    print()

    processed_files = preprocess_s1_scenes(data_dir, scene_ids)

    if processed_files:
        print(f"\n预处理文件保存在: {data_dir / 'cloud_masked'}")


if __name__ == "__main__":
    main()
