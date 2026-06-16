"""
04_cloud_mask.py - SCL 去云处理脚本

功能：
1. 读取波段合成后的 RGB TIF 和对应的 SCL（场景分类层）波段
2. 使用 SCL 分类值识别云、云阴影、卷云等需要去除的像素
3. 将云区域像素设为 NoData（0）
4. 输出去云后的 TIF 文件

SCL 分类值说明（Sentinel-2 L2A）：
  0  = NO_DATA（无数据）
  1  = SATURATED_OR_DEFECTIVE（饱和/缺陷像素）
  2  = DARK_AREA_PIXELS（暗区像素）
  3  = CLOUD_SHADOWS（云阴影）← 需去除
  4  = VEGETATION（植被）
  5  = NOT_VEGETATED（非植被/裸土）
  6  = WATER（水体）
  7  = UNCLASSIFIED（未分类）
  8  = CLOUD_MEDIUM_PROBABILITY（云-中等概率）← 需去除
  9  = CLOUD_HIGH_PROBABILITY（云-高概率）← 需去除
  10 = THIN_CIRRUS（薄卷云）← 需去除
  11 = SNOW（雪/冰）

分辨率说明：
- RGB 波段（B02/B03/B04）分辨率为 10m
- SCL 波段分辨率为 20m
- 需要将 SCL 重采样到 10m 以匹配 RGB
- 使用最近邻重采样（保持分类值不变）

使用方法：
    python scripts/04_cloud_mask.py --data-dir ./data
"""

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    CLOUD_MASKED_DIR,
    CLOUD_SCL_VALUES,
    DOWNLOADS_DIR,
    INVALID_SCL_VALUES,
    MERGED_DIR,
)


def find_scl_file(scene_id: str, downloads_dir: Path) -> Path:
    """
    查找场景对应的 SCL 波段文件。

    文件名格式: {scene_id}_{date}_{cloud_cover}_SCL.tif

    Args:
        scene_id: 场景 ID
        downloads_dir: 下载目录

    Returns:
        Path: SCL 文件路径，未找到则返回 None
    """
    # 使用通配符匹配（因为日期和云量在文件名中）
    pattern = f"{scene_id}_*_SCL.tif"
    matches = list(downloads_dir.glob(pattern))

    if matches:
        return matches[0]

    # 尝试更宽松的匹配
    for tif_file in downloads_dir.glob("*_SCL.tif"):
        if scene_id in tif_file.name:
            return tif_file

    return None


def resample_scl_to_match(
    scl_path: Path,
    target_shape: tuple,
    target_transform: rasterio.Affine,
    target_crs: str,
) -> np.ndarray:
    """
    将 SCL 波段重采样到与 RGB 波段匹配的分辨率。

    SCL 原始分辨率为 20m，RGB 为 10m。
    使用最近邻重采样以保持分类值的完整性（不能用双线性等插值方法）。

    Args:
        scl_path: SCL 文件路径
        target_shape: 目标尺寸 (height, width)
        target_transform: 目标仿射变换
        target_crs: 目标坐标参考系统

    Returns:
        np.ndarray: 重采样后的 SCL 数据
    """
    with rasterio.open(scl_path) as src:
        # 使用 rasterio 的 reproject 进行重采样
        # 最近邻法（nearest）适合分类数据，不会产生新的分类值
        scl_data = src.read(
            1,
            out_shape=target_shape,
            resampling=Resampling.nearest,
        )
    return scl_data


def apply_cloud_mask(
    rgb_path: Path,
    scl_path: Path,
    output_dir: Path,
    cloud_values: list = None,
    invalid_values: list = None,
) -> Path:
    """
    对 RGB TIF 应用 SCL 云掩膜。

    处理流程：
    1. 读取 RGB TIF（3通道）
    2. 读取并重采样 SCL 到 RGB 分辨率
    3. 生成云掩膜（云、云阴影、卷云）
    4. 将云区域像素设为 0（NoData）
    5. 保存去云后的 TIF

    Args:
        rgb_path: RGB TIF 文件路径
        scl_path: SCL 文件路径
        output_dir: 输出目录
        cloud_values: 需要去除的 SCL 值列表（默认使用配置文件中的值）
        invalid_values: 无效数据的 SCL 值列表（默认使用配置文件中的值）

    Returns:
        Path: 去云后的 TIF 文件路径
    """
    if cloud_values is None:
        cloud_values = CLOUD_SCL_VALUES
    if invalid_values is None:
        invalid_values = INVALID_SCL_VALUES

    # 输出文件名：在原文件名基础上添加 _cloudmasked 后缀
    output_filename = rgb_path.stem + "_cloudmasked.tif"
    output_path = output_dir / output_filename

    # 如果已存在则跳过
    if output_path.exists():
        print(f"  [跳过] {output_filename} 已存在")
        return output_path

    print(f"  [去云] 处理: {rgb_path.name}")

    # 1. 读取 RGB 数据
    with rasterio.open(rgb_path) as src:
        rgb_data = src.read()  # shape: (3, height, width)
        profile = src.profile.copy()
        target_shape = (src.height, src.width)
        target_transform = src.transform
        target_crs = src.crs

    # 2. 读取并重采样 SCL
    print(f"  [去云] 重采样 SCL 从 20m 到 10m...")
    scl_data = resample_scl_to_match(
        scl_path, target_shape, target_transform, target_crs
    )

    # 3. 生成云掩膜
    # 云掩膜：SCL 值在 cloud_values 中的像素为 True（需要去除）
    all_bad_values = cloud_values + invalid_values
    cloud_mask = np.isin(scl_data, all_bad_values)

    # 统计云像素占比
    total_pixels = cloud_mask.size
    cloud_pixels = np.sum(cloud_mask)
    cloud_percentage = (cloud_pixels / total_pixels) * 100
    print(f"  [去云] 云/无效像素占比: {cloud_percentage:.1f}% ({cloud_pixels}/{total_pixels})")

    # 4. 应用掩膜：将云区域设为 0
    # 扩展 mask 到 3 通道：(3, height, width)
    cloud_mask_3d = np.broadcast_to(cloud_mask, rgb_data.shape)
    rgb_data[cloud_mask_3d] = 0

    # 5. 保存去云后的 TIF
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(rgb_data)

    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  [完成] {output_filename} ({file_size_mb:.1f} MB)")

    return output_path


def process_all_merged_scenes(data_dir: Path) -> list:
    """
    对所有合成后的 RGB TIF 执行去云处理。

    流程：
    1. 扫描 merged 目录中的 RGB TIF
    2. 从文件名提取 scene_id
    3. 在 downloads 目录中查找对应的 SCL 文件
    4. 应用云掩膜

    Args:
        data_dir: 数据目录

    Returns:
        list: 去云后的 TIF 文件路径列表
    """
    merged_dir = data_dir / "merged"
    downloads_dir = data_dir / "downloads"
    cloud_masked_dir = data_dir / "cloud_masked"

    # 确保输出目录存在
    cloud_masked_dir.mkdir(parents=True, exist_ok=True)

    # 扫描合并后的 RGB TIF
    rgb_files = sorted(merged_dir.glob("*_RGB.tif"))

    if not rgb_files:
        print("[去云] 未找到合成后的 RGB TIF 文件")
        print(f"  请检查目录: {merged_dir}")
        return []

    print(f"[去云] 找到 {len(rgb_files)} 个 RGB TIF 文件")

    # 从文件名提取 scene_id 的正则表达式
    # 文件名格式: {scene_id}_{date}_{cloud_cover}_RGB.tif
    scene_id_pattern = re.compile(r"^(.+?)_\d{8}_[\d.]+_RGB\.tif$")

    masked_files = []
    for rgb_path in rgb_files:
        match = scene_id_pattern.match(rgb_path.name)
        if not match:
            print(f"  [警告] 无法解析文件名: {rgb_path.name}")
            continue

        scene_id = match.group(1)

        # 查找对应的 SCL 文件
        scl_path = find_scl_file(scene_id, downloads_dir)
        if scl_path is None:
            print(f"  [警告] 未找到 {scene_id} 的 SCL 文件，跳过去云处理")
            # 直接复制 RGB 文件到输出目录
            import shutil
            dest = cloud_masked_dir / rgb_path.name
            if not dest.exists():
                shutil.copy2(rgb_path, dest)
            masked_files.append(dest)
            continue

        # 应用云掩膜
        result = apply_cloud_mask(rgb_path, scl_path, cloud_masked_dir)
        if result:
            masked_files.append(result)

    print(f"\n[去云] 完成！共处理 {len(masked_files)}/{len(rgb_files)} 个场景")
    return masked_files


def main():
    """主函数：解析参数并执行去云处理。"""
    parser = argparse.ArgumentParser(
        description="Sentinel-2 SCL 去云处理工具",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="数据目录（包含 merged 和 downloads 子目录）",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    print("=" * 60)
    print("RS-Platform: SCL 去云处理")
    print("=" * 60)

    masked_files = process_all_merged_scenes(data_dir)

    if masked_files:
        print(f"\n去云文件保存在: {data_dir / 'cloud_masked'}")


if __name__ == "__main__":
    main()
