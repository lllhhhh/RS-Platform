"""
08_s1_preprocess.py - Sentinel-1 GRD 预处理（基于 ESA SNAP / snappy）

功能：
对 Sentinel-1 GRD 影像执行标准预处理流程：
1. 下载并应用轨道文件（Apply Orbit File）
2. 辐射定标（Radiometric Calibration）→ 输出 sigma0
3. 斑点噪声滤波（Speckle Filtering）→ Lee Sigma
4. 地形校正（Terrain Correction）→ 地理编码
5. 转化为分贝值（Linear to dB）

依赖：
- ESA SNAP Desktop（需单独安装）
- esa_snappy（SNAP 的 Python 绑定：pip install esa-snappy）

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


def preprocess_single_tif(input_path: Path, output_path: Path) -> Path:
    """
    对单个 S1 GRD TIF 执行完整的 snappy 预处理链。

    处理流程：
    Read → Apply-Orbit-File → Calibration → Speckle-Filter →
    Terrain-Correction → Write（Linear to dB 通过 TC 的输出 Sigma0 dB 实现）

    Args:
        input_path: 输入 TIF 路径（merged 目录下的 S1 多通道 TIF）
        output_path: 输出 TIF 路径

    Returns:
        Path: 输出文件路径
    """
    import esa_snappy as snappy
    from esa_snappy import ProductIO, GPF

    print(f"  [S1预处理] 读取: {input_path.name}")
    product = ProductIO.readProduct(str(input_path))

    # ========== Step 1: 应用轨道文件 ==========
    # 尝试应用轨道文件；如果产品缺少元数据（从 MPC 下载的 TIF），
    # 此步骤可能失败，会跳过继续后续处理
    print(f"  [S1预处理] Step 1/5: 应用轨道文件...")
    try:
        orbit_params = snappy.HashMap()
        orbit_params.put("orbitType", "Sentinel Precise (Auto Download)")
        orbit_params.put("polyDegree", "3")
        product = GPF.createProduct("Apply-Orbit-File", orbit_params, product)
        print(f"  [S1预处理] 轨道文件应用成功")
    except Exception as e:
        print(f"  [S1预处理] 轨道文件跳过（{e.__class__.__name__}），继续处理...")

    # ========== Step 2: 辐射定标 ==========
    # 输出 sigma0（后向散射系数），VV 和 VH 通道
    print(f"  [S1预处理] Step 2/5: 辐射定标...")
    cal_params = snappy.HashMap()
    cal_params.put("outputSigmaBand", "true")
    cal_params.put("outputBetaBand", "false")
    cal_params.put("outputGammaBand", "false")
    cal_params.put("outputDNBand", "false")
    cal_params.put("sourceBands", "Amplitude_VH,Amplitude_VV")
    product = GPF.createProduct("Calibration", cal_params, product)

    # ========== Step 3: 斑点噪声滤波 ==========
    # Lee Sigma 滤波器，窗口 5x5
    print(f"  [S1预处理] Step 3/5: 斑点噪声滤波...")
    filter_params = snappy.HashMap()
    filter_params.put("filter", "Lee Sigma")
    filter_params.put("filterSizeX", "5")
    filter_params.put("filterSizeY", "5")
    filter_params.put("sigma", "0.9")
    filter_params.put("targetWindowSize", "3x3")
    filter_params.put("estimateENL", "true")
    product = GPF.createProduct("Speckle-Filter", filter_params, product)

    # ========== Step 4: 地形校正 ==========
    # 地理编码，使用 SRTM 3sec DEM
    print(f"  [S1预处理] Step 4/5: 地形校正...")
    tc_params = snappy.HashMap()
    tc_params.put("demName", "SRTM 3Sec")
    tc_params.put("demResamplingMethod", "BILINEAR_INTERPOLATION")
    tc_params.put("imgResamplingMethod", "BILINEAR_INTERPOLATION")
    tc_params.put("pixelSpacingInMeter", "10.0")
    tc_params.put("mapProjection", "EPSG:4326")
    tc_params.put("nodataValueAtSea", "true")
    tc_params.put("maskOutAreaWithoutElevation", "true")
    product = GPF.createProduct("Terrain-Correction", tc_params, product)

    # ========== Step 5: 转化为分贝值 ==========
    # 使用 Band Maths 将线性值转换为 dB: 10 * log10(value)
    print(f"  [S1预处理] Step 5/5: 转化为分贝值...")
    band_names = list(product.getBandNames())
    sigma_bands = [b for b in band_names if "Sigma0" in b]

    if sigma_bands:
        for band_name in sigma_bands:
            expression = f"10 * log10({band_name})"
            bm_params = snappy.HashMap()
            bm_params.put("name", f"{band_name}_db")
            bm_params.put("expression", expression)
            product = GPF.createProduct("BandMaths", bm_params, product)

    # ========== 保存输出 ==========
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ProductIO.writeProduct(product, str(output_path), "GeoTIFF")
    product.dispose()

    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  [完成] {output_path.name} ({file_size_mb:.1f} MB)")

    return output_path


def preprocess_s1_scenes(data_dir: Path, scene_ids: set = None) -> list:
    """
    对所有 S1 GRD 合成后的 TIF 执行 snappy 预处理。

    处理流程：
    1. 扫描 merged/ 目录下 *_S1_merged.tif
    2. 对每个文件执行 snappy 预处理链
    3. 输出到 cloud_masked/ 目录（复用后续管线流程）

    Args:
        data_dir: 数据目录
        scene_ids: 要处理的 scene_id 集合，为 None 时处理全部

    Returns:
        list: 预处理后的 TIF 文件路径列表
    """
    import re
    from scripts.orbit_downloader import ensure_orbit_files

    merged_dir = data_dir / "merged"
    cloud_masked_dir = data_dir / "cloud_masked"

    cloud_masked_dir.mkdir(parents=True, exist_ok=True)

    # 扫描 S1 合成后的 TIF
    tif_files = sorted(merged_dir.glob("*_S1_merged.tif"))

    # 预下载轨道文件
    if tif_files:
        dates = set()
        for tif_file in tif_files:
            # 从文件名提取日期（格式: ..._YYYYMMDD_...）
            match = re.search(r'_(\d{8})_', tif_file.name)
            if match:
                d = match.group(1)
                dates.add(f"{d[:4]}-{d[4:6]}-{d[6:8]}")
        if dates:
            print(f"[S1预处理] 检查轨道文件（{len(dates)} 个日期）...")
            ensure_orbit_files(sorted(dates), platform="S1A")

    if not tif_files:
        print("[S1预处理] 未找到 S1 合成 TIF 文件")
        print(f"  请检查目录: {merged_dir}")
        return []

    # 过滤：只处理指定的 scene_id
    if scene_ids is not None:
        tif_files = [f for f in tif_files if any(sid in f.name for sid in scene_ids)]
        print(f"[S1预处理] 过滤后保留 {len(tif_files)} 个场景")
    else:
        print(f"[S1预处理] 找到 {len(tif_files)} 个 S1 TIF 文件")

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
            result = preprocess_single_tif(tif_file, output_path)
            if result:
                processed_files.append(result)
        except Exception as e:
            print(f"  [错误] 处理 {tif_file.name} 失败: {e}")
            traceback.print_exc()

    print(f"\n[S1预处理] 完成！共处理 {len(processed_files)}/{len(tif_files)} 个场景")
    return processed_files


def main():
    """主函数：解析参数并执行 S1 预处理。"""
    parser = argparse.ArgumentParser(
        description="Sentinel-1 GRD 预处理工具（基于 ESA SNAP / snappy）",
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
    print("RS-Platform: Sentinel-1 GRD 预处理")
    print("=" * 60)

    processed_files = preprocess_s1_scenes(data_dir)

    if processed_files:
        print(f"\n预处理文件保存在: {data_dir / 'cloud_masked'}")


if __name__ == "__main__":
    main()
