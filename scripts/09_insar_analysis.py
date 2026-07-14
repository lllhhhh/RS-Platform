"""
09_insar_analysis.py - InSAR 形变监测模块

功能：
使用 ESA SNAP / esa_snappy 对两幅 Sentinel-1 SLC 影像执行 InSAR 处理流程，
生成形变图、相干性图和形变分析报告。

InSAR 处理链：
1. Apply Orbit File — 对主/从影像应用精密轨道文件
2. Back-Geocoding — 将从影像配准到主影像
3. Interferogram Formation — 计算干涉相位
4. Topographic Phase Removal — 去除地形相位分量
5. Goldstein Phase Filtering — 相位滤波降噪
6. Terrain-Correction — 地理编码输出

使用方法：
    # 交互式选择主从影像
    python scripts/09_insar_analysis.py --data-dir ./data --polarization vv

    # 直接指定主从影像
    python scripts/09_insar_analysis.py --master path/master.tif --slave path/slave.tif --polarization vv
"""

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def list_slc_scenes(data_dir: Path) -> list:
    """
    列出 mosaicked 目录下可用的 SLC 裁剪影像。

    Args:
        data_dir: 数据目录

    Returns:
        list: SLC 裁剪影像路径列表
    """
    mosaicked_dir = data_dir / "mosaicked"
    if not mosaicked_dir.exists():
        return []

    # SLC 裁剪后的文件名格式: *_clipped.tif
    slc_files = sorted(mosaicked_dir.glob("*_clipped.tif"))
    return slc_files


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
        print(f"  {i + 1}. {f.name}")

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

    snappy 处理链：
    Read → Apply-Orbit-File → Back-Geocoding → Interferogram-Formation →
    TopoPhaseRemoval → Goldstein-Filter → Terrain-Correction → 导出

    Args:
        master_path: 主影像路径
        slave_path: 从影像路径
        polarization: 极化通道（"vv" 或 "vh"）
        output_dir: 输出目录

    Returns:
        dict: 处理结果信息
    """
    from esa_snappy import ProductIO, GPF, HashMap

    if output_dir is None:
        output_dir = master_path.parent.parent / "insar"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 生成输出文件名前缀
    master_stem = master_path.stem.replace("_clipped", "")
    slave_stem = slave_path.stem.replace("_clipped", "")
    prefix = f"ifg_{master_stem}_vs_{slave_stem}"

    print(f"\n{'=' * 60}")
    print(f"InSAR 处理开始")
    print(f"{'=' * 60}")
    print(f"主影像: {master_path.name}")
    print(f"从影像: {slave_path.name}")
    print(f"极化通道: {polarization.upper()}")
    print(f"输出目录: {output_dir}")
    print(f"{'=' * 60}")

    # ========== Step 1: 读取主从影像 ==========
    print("\n[InSAR] Step 1/6: 读取主从影像...")
    master_product = ProductIO.readProduct(str(master_path))
    slave_product = ProductIO.readProduct(str(slave_path))

    # 选择极化通道波段
    master_bands = list(master_product.getBandNames())
    print(f"  主影像波段: {master_bands}")

    # 如果有多波段，按极化通道选择
    pol_upper = polarization.upper()
    selected_master_bands = [b for b in master_bands if pol_upper in b.upper()]
    selected_slave_bands = [
        b for b in slave_product.getBandNames() if pol_upper in b.upper()
    ]

    if selected_master_bands and selected_slave_bands:
        print(f"  选择极化通道: {pol_upper}")
        # 使用 BandSelect 选择特定波段
        band_select_params = HashMap()
        master_band_list = ",".join(selected_master_bands)
        slave_band_list = ",".join(selected_slave_bands)
        band_select_params.put(
            "selectedPolarisations", pol_upper
        )
        master_product = GPF.createProduct(
            "Select-Product-Band", band_select_params, master_product
        )
        slave_product = GPF.createProduct(
            "Select-Product-Band", band_select_params, slave_product
        )

    # ========== Step 2: 应用轨道文件 ==========
    print("[InSAR] Step 2/6: 应用轨道文件...")
    orbit_params = HashMap()
    orbit_params.put("orbitType", "Sentinel Precise (Auto Download)")
    orbit_params.put("polyDegree", "3")

    try:
        master_product = GPF.createProduct(
            "Apply-Orbit-File", orbit_params, master_product
        )
        print("  主影像轨道文件应用成功")
    except Exception as e:
        print(f"  主影像轨道文件跳过: {e.__class__.__name__}")

    try:
        slave_product = GPF.createProduct(
            "Apply-Orbit-File", orbit_params, slave_product
        )
        print("  从影像轨道文件应用成功")
    except Exception as e:
        print(f"  从影像轨道文件跳过: {e.__class__.__name__}")

    # ========== Step 3: 配准（Back-Geocoding）==========
    print("[InSAR] Step 3/6: 影像配准（Back-Geocoding）...")
    bg_params = HashMap()
    bg_params.put("demName", "SRTM 3Sec")
    bg_params.put("demResamplingMethod", "BILINEAR_INTERPOLATION")
    bg_params.put("maskOutAreaWithoutElevation", "true")

    source_products = HashMap()
    source_products.put("master", master_product)
    source_products.put("slave", slave_product)
    coregistered = GPF.createProduct("Back-Geocoding", bg_params, source_products)

    # ========== Step 4: 干涉图生成 + 地形相位去除 ==========
    print("[InSAR] Step 4/6: 生成干涉图 + 去除地形相位...")

    # 干涉图生成
    ifg_params = HashMap()
    ifg_params.put("subtractFlatEarthPhase", "true")
    ifg_params.put("srpPolynomialDegree", "5")
    ifg_params.put("srpNumberPoints", "501")
    ifg_params.put("orbitDegree", "3")
    ifg_params.put("includeCoherence", "true")
    interferogram = GPF.createProduct(
        "Interferogram-Formation", ifg_params, coregistered
    )

    # 地形相位去除
    topo_params = HashMap()
    topo_params.put("orbitDegree", "3")
    topo_params.put("demName", "SRTM 3Sec")
    topo_params.put("demResamplingMethod", "BILINEAR_INTERPOLATION")
    interferogram = GPF.createProduct(
        "TopoPhaseRemoval", topo_params, interferogram
    )

    # ========== Step 5: Goldstein 相位滤波 ==========
    print("[InSAR] Step 5/6: Goldstein 相位滤波...")
    filter_params = HashMap()
    filter_params.put("alpha", "0.5")
    filter_params.put("FFTSizeString", "64")
    filter_params.put("windowSizeString", "3")
    filter_params.put("useCoherenceMask", "false")
    filtered = GPF.createProduct("GoldsteinPhaseFilter", filter_params, interferogram)

    # ========== Step 6: 地形校正（地理编码）==========
    print("[InSAR] Step 6/6: 地形校正（Terrain-Correction）...")
    tc_params = HashMap()
    tc_params.put("demName", "SRTM 3Sec")
    tc_params.put("demResamplingMethod", "BILINEAR_INTERPOLATION")
    tc_params.put("imgResamplingMethod", "BILINEAR_INTERPOLATION")
    tc_params.put("pixelSpacingInMeter", "10.0")
    tc_params.put("mapProjection", "EPSG:4326")
    tc_params.put("nodataValueAtSea", "true")
    tc_params.put("maskOutAreaWithoutElevation", "true")
    result = GPF.createProduct("Terrain-Correction", tc_params, filtered)

    # ========== 导出 GeoTIFF ==========
    print("\n[InSAR] 导出 GeoTIFF...")

    # 导出完整结果（包含相位、相干性等所有波段）
    output_prefix = output_dir / prefix
    result_path = output_dir / f"{prefix}_full.tif"
    ProductIO.writeProduct(result, str(result_path), "GeoTIFF")
    print(f"  完整产品: {result_path.name}")

    # 获取结果波段信息
    result_bands = list(result.getBandNames())
    print(f"  输出波段: {result_bands}")

    # 提取各分量
    output_files = {}

    # 相干性
    coherence_bands = [b for b in result_bands if "coh" in b.lower() or "coherence" in b.lower()]
    if coherence_bands:
        coh_path = output_dir / f"{prefix}_coherence.tif"
        coh_params = HashMap()
        coh_params.put("sourceBands", coherence_bands[0])
        coh_product = GPF.createProduct("Subset", coh_params, result)
        ProductIO.writeProduct(coh_product, str(coh_path), "GeoTIFF")
        output_files["coherence"] = str(coh_path)
        print(f"  相干性图: {coh_path.name}")

    # 干涉相位
    phase_bands = [b for b in result_bands if "phase" in b.lower() or "ifg" in b.lower()]
    if phase_bands:
        phase_path = output_dir / f"{prefix}_phase.tif"
        phase_params = HashMap()
        phase_params.put("sourceBands", phase_bands[0])
        phase_product = GPF.createProduct("Subset", phase_params, result)
        ProductIO.writeProduct(phase_product, str(phase_path), "GeoTIFF")
        output_files["phase"] = str(phase_path)
        print(f"  相位图: {phase_path.name}")

    # 形变（如果存在 displacement 波段）
    disp_bands = [b for b in result_bands if "displacement" in b.lower() or "deformation" in b.lower()]
    if disp_bands:
        disp_path = output_dir / f"{prefix}_deformation.tif"
        disp_params = HashMap()
        disp_params.put("sourceBands", disp_bands[0])
        disp_product = GPF.createProduct("Subset", disp_params, result)
        ProductIO.writeProduct(disp_product, str(disp_path), "GeoTIFF")
        output_files["deformation"] = str(disp_path)
        print(f"  形变图: {disp_path.name}")

    # ========== 计算统计信息 ==========
    print("\n[InSAR] 计算形变分析统计...")
    stats = compute_insar_stats(result, result_bands, output_files)

    # 保存处理参数
    stats["processing_info"] = {
        "master": master_path.name,
        "slave": slave_path.name,
        "polarization": polarization,
        "dem": "SRTM 3Sec",
        "processed_at": datetime.now().isoformat(),
    }

    # 导出报告
    report_path = output_dir / f"{prefix}_report.json"
    export_report(stats, report_path)

    # 清理
    master_product.dispose()
    slave_product.dispose()
    result.dispose()

    print(f"\n{'=' * 60}")
    print(f"InSAR 处理完成！")
    print(f"{'=' * 60}")
    print(f"输出目录: {output_dir}")
    for name, path in output_files.items():
        print(f"  {name}: {Path(path).name}")
    print(f"  报告: {report_path.name}")

    return {"output_dir": str(output_dir), "files": output_files, "report": str(report_path)}


def compute_insar_stats(result_product, band_names: list, output_files: dict) -> dict:
    """
    计算 InSAR 结果的统计信息。

    Args:
        result_product: snappy 处理结果产品
        band_names: 波段名称列表
        output_files: 输出文件路径字典

    Returns:
        dict: 统计信息
    """
    import rasterio

    stats = {
        "deformation": {},
        "coherence": {},
    }

    # 从输出文件读取并计算统计
    if "deformation" in output_files:
        try:
            with rasterio.open(output_files["deformation"]) as src:
                data = src.read(1)
                valid = data[~np.isnan(data) & (data != 0)]
                if valid.size > 0:
                    stats["deformation"] = {
                        "mean_m": float(np.mean(valid)),
                        "std_m": float(np.std(valid)),
                        "max_uplift_m": float(np.max(valid)),
                        "max_subsidence_m": float(np.min(valid)),
                        "median_m": float(np.median(valid)),
                        "valid_pixels": int(valid.size),
                        "total_pixels": int(data.size),
                    }
        except Exception as e:
            print(f"  [警告] 形变统计计算失败: {e}")

    if "coherence" in output_files:
        try:
            with rasterio.open(output_files["coherence"]) as src:
                data = src.read(1)
                valid = data[~np.isnan(data) & (data != 0)]
                if valid.size > 0:
                    low_coherence = valid[valid < 0.3]
                    stats["coherence"] = {
                        "mean": float(np.mean(valid)),
                        "std": float(np.std(valid)),
                        "low_coherence_ratio": float(len(low_coherence) / valid.size),
                        "valid_pixels": int(valid.size),
                    }
        except Exception as e:
            print(f"  [警告] 相干性统计计算失败: {e}")

    # 如果没有形变波段，尝试从完整产品中读取相位并估算
    if not stats["deformation"] and "phase" in output_files:
        try:
            with rasterio.open(output_files["phase"]) as src:
                data = src.read(1)
                valid = data[~np.isnan(data) & (data != 0)]
                if valid.size > 0:
                    wavelength = 0.055465763  # Sentinel-1 C 波段波长（米）
                    deformation = -wavelength / (4 * np.pi) * valid
                    stats["deformation"] = {
                        "mean_m": float(np.mean(deformation)),
                        "std_m": float(np.std(deformation)),
                        "max_uplift_m": float(np.max(deformation)),
                        "max_subsidence_m": float(np.min(deformation)),
                        "median_m": float(np.median(deformation)),
                        "valid_pixels": int(valid.size),
                        "note": "从干涉相位估算（C 波段波长 5.55cm）",
                    }
        except Exception as e:
            print(f"  [警告] 相位转形变计算失败: {e}")

    return stats


def export_report(stats: dict, output_path: Path) -> None:
    """
    导出 InSAR 分析报告（JSON 格式）。

    Args:
        stats: 统计信息字典
        output_path: 输出文件路径
    """
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"  分析报告: {output_path.name}")


def main():
    """主函数：解析参数并执行 InSAR 分析。"""
    parser = argparse.ArgumentParser(
        description="InSAR 形变监测工具（基于 ESA SNAP / snappy）",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="数据目录",
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
        help="输出目录（默认 data/insar）",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir) if args.output_dir else data_dir / "insar"

    print("=" * 60)
    print("RS-Platform: InSAR 形变监测")
    print("=" * 60)

    # 确定主从影像
    if args.master and args.slave:
        master_path = Path(args.master)
        slave_path = Path(args.slave)
    else:
        slc_files = list_slc_scenes(data_dir)
        if len(slc_files) < 2:
            print(f"[错误] 需要至少 2 幅 SLC 裁剪影像，当前找到 {len(slc_files)} 幅")
            print(f"  请先通过管线下载并裁剪 SLC 影像：")
            print(f"  python scripts/06_pipeline.py --satellite sentinel1 --s1-product slc --bbox ...")
            return

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
    except Exception as e:
        print(f"\n[错误] InSAR 处理失败: {e}")
        traceback.print_exc()
        return

    print(f"\n处理完成！请查看输出目录: {result['output_dir']}")


if __name__ == "__main__":
    main()
