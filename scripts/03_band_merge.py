"""
03_band_merge.py - 波段合成脚本

功能：
1. 扫描下载目录，按 scene_id 分组已下载的单波段 TIF 文件
2. 将 B02(蓝)、B03(绿)、B04(红) 三个波段合成为 RGB 三通道 TIF
3. 输出文件名携带关键元信息（scene_id、日期、云量）
4. 保留原始 CRS、Transform、NoData 等空间参考信息

波段顺序说明：
- 通道1: B04 (Red, 红色)
- 通道2: B03 (Green, 绿色)
- 通道3: B02 (Blue, 蓝色)
这是标准的 RGB 波段排列，可直接用 GIS 软件打开显示真彩色。

使用方法：
    python scripts/03_band_merge.py --data-dir ./data
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    DOWNLOADS_DIR,
    MERGED_DIR,
    RGB_BAND_ORDER,
    S1_BAND_ORDER,
)


def scan_downloaded_bands(downloads_dir: Path) -> dict:
    """
    扫描下载目录，按 scene_id 分组所有已下载的波段文件。

    文件名格式: {scene_id}_{date}_{cloud_cover}_{band}.tif
    通过正则表达式解析文件名，提取 scene_id、日期、云量和波段名。

    Args:
        downloads_dir: 下载目录路径

    Returns:
        dict: {scene_id: {band_name: file_path, ...}, ...}
    """
    # 匹配文件名格式的正则表达式
    # S2 示例: S2A_MSIL2A_20240115T031111_N0510_R075_T50TLK_20240115T061738_20240115_12.5_B04.tif
    # S1 示例: S1A_IW_GRDH_20240101T000000_20240101T000029_051000_062000_0001_20240101_0.0_vv.tif
    pattern = re.compile(
        r"^(.+)_(\d{8})_([\d.]+)_(B02|B03|B04|SCL|vv|vh)\.tif$"
    )

    # 按 scene_id 分组
    scenes = defaultdict(dict)

    for tif_file in downloads_dir.glob("*.tif"):
        match = pattern.match(tif_file.name)
        if match:
            scene_id = match.group(1)
            date = match.group(2)
            cloud_cover = match.group(3)
            band_name = match.group(4)

            scenes[scene_id][band_name] = {
                "path": tif_file,
                "date": date,
                "cloud_cover": cloud_cover,
            }

    print(f"[波段合成] 扫描到 {len(scenes)} 个场景")
    return dict(scenes)


def merge_rgb_bands(
    scene_id: str,
    band_files: dict,
    output_dir: Path,
) -> Path:
    """
    将单波段 TIF 合成为 RGB 三通道 TIF。

    合成流程：
    1. 以 B04(红) 波段为基准，获取空间参考信息（CRS、Transform、尺寸）
    2. 按 B04→B03→B02 顺序读取三个波段数据
    3. 写入一个 3 通道的 TIF 文件

    Args:
        scene_id: 场景 ID
        band_files: 波段文件信息字典
        output_dir: 输出目录

    Returns:
        Path: 合成后的 TIF 文件路径，如果缺少波段则返回 None
    """
    # 检查是否三个 RGB 波段都存在
    missing_bands = [b for b in RGB_BAND_ORDER if b not in band_files]
    if missing_bands:
        print(f"  [跳过] {scene_id}: 缺少波段 {missing_bands}")
        return None

    # 从元数据中提取日期和云量
    first_band = list(band_files.values())[0]
    date = first_band["date"]
    cloud_cover = first_band["cloud_cover"]

    # 输出文件名
    output_filename = f"{scene_id}_{date}_{cloud_cover}_RGB.tif"
    output_path = output_dir / output_filename

    # 如果已存在则跳过
    if output_path.exists():
        print(f"  [跳过] {output_filename} 已存在")
        return output_path

    print(f"  [合成] {scene_id}: 合成 RGB 波段...")

    # 以 B04(红) 波段为基准读取空间参考信息
    with rasterio.open(band_files["B04"]["path"]) as src:
        profile = src.profile.copy()
        b04_data = src.read(1)
        height, width = b04_data.shape

    # 读取 B03(绿) 和 B02(蓝) 波段
    with rasterio.open(band_files["B03"]["path"]) as src:
        b03_data = src.read(1)

    with rasterio.open(band_files["B02"]["path"]) as src:
        b02_data = src.read(1)

    # 检查波段尺寸是否一致
    if b03_data.shape != (height, width) or b02_data.shape != (height, width):
        print(f"  [错误] {scene_id}: 波段尺寸不一致")
        return None

    # 更新 profile：3 个波段
    profile.update(count=3)

    # 按 RGB 顺序写入（通道1=Red/B04, 通道2=Green/B03, 通道3=Blue/B02）
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(b04_data, 1)  # Red
        dst.write(b03_data, 2)  # Green
        dst.write(b02_data, 3)  # Blue

    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  [完成] {output_filename} ({file_size_mb:.1f} MB)")

    return output_path


def merge_s1_bands(
    scene_id: str,
    band_files: dict,
    output_dir: Path,
) -> Path:
    """
    将 Sentinel-1 GRD 的 vv 和 vh 两个极化通道合成为双通道 TIF。

    Args:
        scene_id: 场景 ID
        band_files: 波段文件信息字典
        output_dir: 输出目录

    Returns:
        Path: 合成后的 TIF 文件路径，如果缺少波段则返回 None
    """
    missing_bands = [b for b in S1_BAND_ORDER if b not in band_files]
    if missing_bands:
        print(f"  [跳过] {scene_id}: 缺少极化通道 {missing_bands}")
        return None

    first_band = list(band_files.values())[0]
    date = first_band["date"]
    cloud_cover = first_band.get("cloud_cover", "0.0")

    output_filename = f"{scene_id}_{date}_{cloud_cover}_S1_merged.tif"
    output_path = output_dir / output_filename

    if output_path.exists():
        print(f"  [跳过] {output_filename} 已存在")
        return output_path

    print(f"  [合成] {scene_id}: 合成 vv+vh 极化通道...")

    # 以 vv 通道为基准读取空间参考信息
    with rasterio.open(band_files["vv"]["path"]) as src:
        profile = src.profile.copy()
        vv_data = src.read(1)
        height, width = vv_data.shape

    with rasterio.open(band_files["vh"]["path"]) as src:
        vh_data = src.read(1)

    if vh_data.shape != (height, width):
        print(f"  [错误] {scene_id}: vv 和 vh 尺寸不一致")
        return None

    profile.update(count=2)

    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(vv_data, 1)  # VV
        dst.write(vh_data, 2)  # VH

    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  [完成] {output_filename} ({file_size_mb:.1f} MB)")

    return output_path


def merge_all_scenes(data_dir: Path, scene_ids: set = None) -> list:
    """
    对已下载场景执行波段合成。

    Args:
        data_dir: 数据目录
        scene_ids: 要处理的 scene_id 集合，为 None 时处理全部

    Returns:
        list: 所有合成后的 TIF 文件路径
    """
    downloads_dir = data_dir / "downloads"
    merged_dir = data_dir / "merged"

    # 确保输出目录存在
    merged_dir.mkdir(parents=True, exist_ok=True)

    # 读取 metadata 确定卫星类型
    metadata_path = data_dir / "metadata.json"
    metadata = {}
    satellite = "sentinel2"
    s1_product = "grd"
    if metadata_path.exists():
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        satellite = metadata.get("satellite", "sentinel2")
        s1_product = metadata.get("s1_product", "grd")

    # S1 SLC（CDSE）：直接从 metadata 读取波段路径
    if satellite == "sentinel1" and s1_product == "slc" and metadata.get("scenes"):
        print("[波段合成] S1 SLC 模式：从 SAFE 提取的波段合成 vv+vh")
        merged_files = []
        for scene in metadata["scenes"]:
            scene_id = scene["scene_id"]
            bands_info = scene.get("bands", {})

            # 检查 vv 和 vh 是否都存在
            band_files = {}
            for pol_name in S1_BAND_ORDER:
                if pol_name in bands_info:
                    local_path = Path(bands_info[pol_name]["local_path"])
                    if local_path.exists():
                        band_files[pol_name] = {
                            "path": local_path,
                            "date": scene.get("date", ""),
                            "cloud_cover": "0.0",
                        }

            if len(band_files) == len(S1_BAND_ORDER):
                result = merge_s1_bands(scene_id, band_files, merged_dir)
                if result:
                    merged_files.append(result)
            else:
                print(f"  [跳过] {scene_id}: 缺少波段 {set(S1_BAND_ORDER) - set(band_files)}")

        print(f"\n[波段合成] 完成！共合成 {len(merged_files)}/{len(metadata['scenes'])} 个场景")
        return merged_files

    # S1 GRD / S2：扫描 downloads 目录
    scenes = scan_downloaded_bands(downloads_dir)

    if not scenes:
        print("[波段合成] 未找到已下载的波段文件")
        print(f"  请检查下载目录: {downloads_dir}")
        return []

    # 过滤：只处理指定的 scene_id
    if scene_ids is not None:
        scenes = {sid: bands for sid, bands in scenes.items() if sid in scene_ids}
        print(f"[波段合成] 过滤后保留 {len(scenes)} 个场景")

    # 根据卫星类型选择合成函数
    if satellite == "sentinel1":
        print("[波段合成] Sentinel-1 GRD 模式：合成 vv+vh")
        merge_func = merge_s1_bands
    else:
        merge_func = merge_rgb_bands

    # 对每个场景执行合成
    merged_files = []
    for scene_id, band_files in scenes.items():
        result = merge_func(scene_id, band_files, merged_dir)
        if result:
            merged_files.append(result)

    print(f"\n[波段合成] 完成！共合成 {len(merged_files)}/{len(scenes)} 个场景")
    return merged_files


def main():
    """主函数：解析参数并执行波段合成。"""
    parser = argparse.ArgumentParser(
        description="Sentinel-2 波段合成工具（B02+B03+B04 → RGB TIF）",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="数据目录（包含 downloads 子目录）",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    print("=" * 60)
    print("RS-Platform: 波段合成")
    print("=" * 60)

    merged_files = merge_all_scenes(data_dir)

    if merged_files:
        print(f"\n合成文件保存在: {data_dir / 'merged'}")


if __name__ == "__main__":
    main()
