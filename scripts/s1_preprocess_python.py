"""
s1_preprocess_python.py - Sentinel-1 GRD 预处理（纯 Python 实现）

功能：
使用 sarsen 库对 Sentinel-1 GRD 数据执行标准预处理流程：
1. 读取 GRD 数据（通过 xarray-sentinel）
2. 应用轨道文件（自动下载）
3. 地形校正（几何校正）
4. 辐射校正（gamma flattening）
5. 转换为分贝值

依赖：
- sarsen（纯 Python SAR 处理库）
- xarray-sentinel（Sentinel-1 数据读取）
- pygmtsar（DEM 下载，可选）

处理输入：merged/ 目录下的 S1 多通道 TIF（VV+VH）
处理输出：cloud_masked/ 目录下的预处理后 TIF（复用后续管线流程）

使用方法：
    python scripts/s1_preprocess_python.py --data-dir ./data
    python scripts/s1_preprocess_python.py --data-dir ./data --scene-ids S1A_IW_GRDH_...
"""

import argparse
import sys
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def preprocess_grd_with_sarsen(
    grd_path: Path,
    dem_path: Path,
    output_path: Path,
    correct_radiometry: str = "gamma_nearest",
    interp_method: str = "nearest",
) -> Path:
    """
    使用 sarsen 对 Sentinel-1 GRD 数据进行预处理。

    sarsen 处理链：
    1. 读取 GRD 数据（xarray-sentinel）
    2. 应用轨道文件（自动下载）
    3. 地形校正（几何校正）
    4. 辐射校正（gamma flattening，可选）
    5. 输出 GeoTIFF

    Args:
        grd_path: GRD 数据路径（SAFE 目录）
        dem_path: DEM 数据路径（NetCDF 或 GeoTIFF）
        output_path: 输出文件路径
        correct_radiometry: 辐射校正方法
            - None: 不进行辐射校正
            - "gamma_bilinear": 经典 gamma flattening（双线性插值）
            - "gamma_nearest": gamma flattening（最近邻，更快）
        interp_method: 重采样插值方法

    Returns:
        Path: 输出文件路径
    """
    from sarsen import apps

    print(f"  [GRD] 读取数据: {grd_path}")
    print(f"  [GRD] 使用 DEM: {dem_path}")

    # 执行地形校正
    # sarsen 会自动处理：
    # - 读取 GRD 数据
    # - 应用轨道文件
    # - 地形校正（几何校正）
    # - 辐射校正（如果指定）
    print(f"  [GRD] 执行地形校正（correct_radiometry={correct_radiometry}）...")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    terrain_corrected = apps.terrain_correction(
        product=str(grd_path),
        dem_urlpath=str(dem_path),
        output_urlpath=str(output_path),
        correct_radiometry=correct_radiometry,
        interp_method=interp_method,
        chunks=1024,
    )

    print(f"  [GRD] 地形校正完成: {output_path}")
    return output_path


def linear_to_db(data):
    """
    将线性值转换为分贝值。

    Args:
        data: 线性值数组（numpy array 或 xarray DataArray）

    Returns:
        分贝值数组
    """
    return 10 * np.log10(np.maximum(data, 1e-10))


def preprocess_single_tif(
    input_path: Path,
    output_path: Path,
    dem_path: Path = None,
) -> Path:
    """
    对单个 S1 GRD TIF 执行预处理。

    处理流程：
    1. 使用 sarsen 进行地形校正
    2. 转换为分贝值
    3. 保存结果

    Args:
        input_path: 输入 TIF 路径（merged 目录下的 S1 多通道 TIF）
        output_path: 输出 TIF 路径
        dem_path: DEM 文件路径（如果为 None，自动下载）

    Returns:
        Path: 输出文件路径
    """
    from scripts.dem_downloader import download_dem_for_merged_tif

    # 如果没有提供 DEM，自动下载
    if dem_path is None:
        print(f"  [GRD] 下载 DEM...")
        dem_path = download_dem_for_merged_tif(input_path)

    # 使用 sarsen 进行预处理
    result_path = preprocess_grd_with_sarsen(
        grd_path=input_path,
        dem_path=dem_path,
        output_path=output_path,
        correct_radiometry="gamma_nearest",
    )

    # 转换为分贝值
    print(f"  [GRD] 转换为分贝值...")
    import xarray as xr
    import rioxarray

    data = xr.open_dataarray(str(result_path))
    data_db = linear_to_db(data)

    # 保存为分贝值
    data_db.rio.to_raster(str(result_path))

    print(f"  [GRD] 转换完成")
    return result_path


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
    merged_dir = data_dir / "merged"
    cloud_masked_dir = data_dir / "cloud_masked"

    cloud_masked_dir.mkdir(parents=True, exist_ok=True)

    # 扫描 S1 合成后的 TIF
    tif_files = sorted(merged_dir.glob("*_S1_merged.tif"))

    if not tif_files:
        print("[GRD] 未找到 S1 合成 TIF 文件")
        print(f"  请检查目录: {merged_dir}")
        return []

    # 过滤：只处理指定的 scene_id
    if scene_ids is not None:
        tif_files = [f for f in tif_files if any(sid in f.name for sid in scene_ids)]
        print(f"[GRD] 过滤后保留 {len(tif_files)} 个场景")
    else:
        print(f"[GRD] 找到 {len(tif_files)} 个 S1 TIF 文件")

    # 预下载 DEM（使用第一个文件的覆盖范围）
    from scripts.dem_downloader import download_dem_for_merged_tif
    dem_path = None

    processed_files = []
    for tif_file in tif_files:
        # 输出文件名：{原名去掉_S1_merged}_cloudmasked.tif
        output_name = tif_file.name.replace("_S1_merged.tif", "_cloudmasked.tif")
        output_path = cloud_masked_dir / output_name

        # 如果已存在则跳过
        if output_path.exists():
            print(f"  [跳过] {output_name} 已存在")
            processed_files.append(output_path)
            continue

        try:
            # 第一个文件时下载 DEM，后续文件复用
            if dem_path is None:
                print(f"[GRD] 下载 DEM...")
                dem_path = download_dem_for_merged_tif(tif_file)

            result = preprocess_single_tif(tif_file, output_path, dem_path)
            if result:
                processed_files.append(result)
        except Exception as e:
            print(f"  [错误] 处理 {tif_file.name} 失败: {e}")
            traceback.print_exc()

    print(f"\n[GRD] 完成！共处理 {len(processed_files)}/{len(tif_files)} 个场景")
    return processed_files


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

    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    print("=" * 60)
    print("RS-Platform: Sentinel-1 GRD 预处理（纯 Python）")
    print("=" * 60)
    print(f"使用 sarsen 库进行地形校正和辐射校正")
    print()

    processed_files = preprocess_s1_scenes(data_dir)

    if processed_files:
        print(f"\n预处理文件保存在: {data_dir / 'cloud_masked'}")


if __name__ == "__main__":
    main()
