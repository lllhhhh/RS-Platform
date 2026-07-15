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

import os


def _ensure_java_home() -> bool:
    """自动检测并设置 JAVA_HOME（ESA SNAP JRE）。"""
    if os.environ.get("JAVA_HOME"):
        return True

    candidates = [
        r"D:\esa-snap\jre",
        r"C:\esa-snap\jre",
        r"/usr/local/snap/jre",
        r"/opt/snap/jre",
    ]
    for path in candidates:
        if os.path.isdir(path):
            os.environ["JAVA_HOME"] = path
            print(f"[InSAR] 自动设置 JAVA_HOME={path}")
            return True

    return False


def list_slc_scenes(data_dir: Path) -> list:
    """
    列出可用的 SLC 影像。

    优先查找 SAFE 目录（InSAR 需要完整元数据），
    其次查找 merged 目录的 TIF，最后查找 mosaicked 目录。

    Args:
        data_dir: 数据目录

    Returns:
        list: SLC 影像路径列表
    """
    # 优先从 SAFE 目录查找（InSAR 需要完整 SAR 元数据）
    s1_slc_dir = data_dir / "downloads" / "s1_slc"
    if s1_slc_dir.exists():
        safe_dirs = sorted(s1_slc_dir.glob("*.SAFE"))
        if safe_dirs:
            return safe_dirs

    # 其次从 merged 目录查找 SLC 合成影像
    merged_dir = data_dir / "merged"
    if merged_dir.exists():
        slc_files = sorted(merged_dir.glob("*_S1_merged.tif"))
        if slc_files:
            return slc_files

    # 兼容 mosaicked 目录的裁剪影像
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
    if not _ensure_java_home():
        print("[错误] 未找到 JAVA_HOME，esa_snappy 需要 JVM 支持")
        print("  请设置环境变量: set JAVA_HOME=D:\\esa-snap\\jre")
        print("  或安装 ESA SNAP Desktop: https://step.esa.int/")
        sys.exit(1)

    from esa_snappy import ProductIO, GPF, HashMap

    if output_dir is None:
        output_dir = master_path.parent.parent / "insar"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 判断是否为 SAFE 目录
    is_safe = master_path.suffix.upper() == ".SAFE" or (master_path.is_dir() and master_path.name.upper().endswith(".SAFE"))

    # 生成输出文件名前缀
    if is_safe:
        master_stem = master_path.name.replace(".SAFE", "").replace(".safe", "")
        slave_stem = slave_path.name.replace(".SAFE", "").replace(".safe", "")
    else:
        master_stem = master_path.stem.replace("_clipped", "").replace("_S1_merged", "")
        slave_stem = slave_path.stem.replace("_clipped", "").replace("_S1_merged", "")
    # 截取简短名称（取日期部分）
    master_short = master_stem.split("_T")[0] if "_T" in master_stem else master_stem[:50]
    slave_short = slave_stem.split("_T")[0] if "_T" in slave_stem else slave_stem[:50]
    prefix = f"ifg_{master_short}_vs_{slave_short}"

    print(f"\n{'=' * 60}")
    print(f"InSAR 处理开始")
    print(f"{'=' * 60}")
    print(f"主影像: {master_path.name}")
    print(f"从影像: {slave_path.name}")
    print(f"极化通道: {polarization.upper()}")
    print(f"输出目录: {output_dir}")
    print(f"{'=' * 60}")

    # ========== Step 0: 确保轨道文件可用 ==========
    print("\n[InSAR] Step 0/7: 检查轨道文件...")
    from scripts.orbit_downloader import ensure_orbit_files
    import re

    # 从 SAFE 目录名提取日期
    def _extract_date_from_safe(safe_path):
        name = safe_path.name if safe_path.is_dir() else safe_path.stem
        match = re.search(r'(\d{8})T\d{6}', name)
        if match:
            d = match.group(1)
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return None

    master_date = _extract_date_from_safe(master_path)
    slave_date = _extract_date_from_safe(slave_path)

    dates_to_fetch = []
    if master_date:
        dates_to_fetch.append(master_date)
    if slave_date and slave_date != master_date:
        dates_to_fetch.append(slave_date)

    if dates_to_fetch:
        orbit_results = ensure_orbit_files(dates_to_fetch, platform="S1A")
        if orbit_results:
            print(f"  轨道文件就绪: {list(orbit_results.keys())}")
        else:
            print("  [警告] 未能获取轨道文件，InSAR 精度可能受影响")

    # ========== Step 1: 读取主从影像 ==========
    print("\n[InSAR] Step 1/6: 读取主从影像...")
    if is_safe:
        # SAFE 格式：读取 manifest.safe
        manifest_master = master_path / "manifest.safe"
        manifest_slave = slave_path / "manifest.safe"
        print(f"  读取 SAFE 格式: {manifest_master}")
        master_product = ProductIO.readProduct(str(manifest_master))
        slave_product = ProductIO.readProduct(str(manifest_slave))
    else:
        master_product = ProductIO.readProduct(str(master_path))
        slave_product = ProductIO.readProduct(str(slave_path))

    # 显示波段信息
    master_bands = list(master_product.getBandNames())
    slave_bands = list(slave_product.getBandNames())
    print(f"  主影像波段数: {len(master_bands)}")
    print(f"  从影像波段数: {len(slave_bands)}")

    # 显示匹配的极化通道波段
    pol_upper = polarization.upper()
    matched_bands = [b for b in master_bands if pol_upper in b.upper()]
    if matched_bands:
        print(f"  匹配 {pol_upper} 极化波段: {matched_bands[:6]}...")

    # ========== Step 2: 应用轨道文件 ==========
    print("[InSAR] Step 2/6: 应用轨道文件...")
    orbit_params = HashMap()
    orbit_params.put("orbitType", "Sentinel Precise (Auto Download)")
    orbit_params.put("polyDegree", "3")

    orbit_success = False
    try:
        master_product = GPF.createProduct(
            "Apply-Orbit-File", orbit_params, master_product
        )
        print("  主影像轨道文件应用成功")
        orbit_success = True
    except Exception as e:
        print(f"  主影像轨道文件跳过: {e.__class__.__name__}")

    try:
        slave_product = GPF.createProduct(
            "Apply-Orbit-File", orbit_params, slave_product
        )
        print("  从影像轨道文件应用成功")
    except Exception as e:
        print(f"  从影像轨道文件跳过: {e.__class__.__name__}")

    if not orbit_success:
        print("\n  [警告] 轨道文件未能下载，InSAR 精度可能受影响")
        print("  [提示] 可手动下载轨道文件到: C:\\Users\\<用户名>\\.snap\\auxdata\\Orbits\\Sentinel-1\\")
        print("  [提示] 下载地址: https://s1qc.asf.alaska.edu/aux_poeorb/")

    # ========== Step 3: TOPS Split（选择子条带）==========
    subswath = "IW2"
    pol_upper = polarization.upper()
    print(f"[InSAR] Step 3/6: TOPS Split（子条带: {subswath}, 极化: {pol_upper}）...")

    split_params = HashMap()
    split_params.put("subswath", subswath)
    split_params.put("polarization", pol_upper)
    split_params.put("selectedPolarisations", pol_upper)

    master_split = GPF.createProduct("TOPSAR-Split", split_params, master_product)
    slave_split = GPF.createProduct("TOPSAR-Split", split_params, slave_product)
    print(f"  主影像 Split 波段: {list(master_split.getBandNames())}")

    # ========== Step 4: Back-Geocoding + 干涉图 ==========
    print("[InSAR] Step 4/6: Back-Geocoding + 干涉图生成...")

    bg_params = HashMap()
    bg_params.put("demName", "SRTM 3Sec")
    bg_params.put("demResamplingMethod", "BILINEAR_INTERPOLATION")
    bg_params.put("maskOutAreaWithoutElevation", "true")

    source_products = HashMap()
    source_products.put("master", master_split)
    source_products.put("slave", slave_split)
    coregistered = GPF.createProduct("Back-Geocoding", bg_params, source_products)

    ifg_params = HashMap()
    ifg_params.put("subtractFlatEarthPhase", "true")
    ifg_params.put("srpPolynomialDegree", "5")
    ifg_params.put("srpNumberPoints", "501")
    ifg_params.put("orbitDegree", "3")
    ifg_params.put("includeCoherence", "true")
    interferogram = GPF.createProduct("Interferogram", ifg_params, coregistered)

    # ========== Step 5: TOPS Deburst ==========
    print("[InSAR] Step 5/6: TOPS Deburst...")
    deburst_params = HashMap()
    deburst_params.put("selectedPolarisations", pol_upper)
    deburst = GPF.createProduct("TOPSAR-Deburst", deburst_params, interferogram)
    print("  Deburst 完成")

    # ========== Step 6: Goldstein 相位滤波（可选）==========
    print("[InSAR] Step 6/6: Goldstein 相位滤波...")
    filtered = deburst
    filter_params = HashMap()
    filter_params.put("alpha", "0.5")
    filter_params.put("FFTSizeString", "64")
    filter_params.put("windowSizeString", "3")
    filter_params.put("useCoherenceMask", "false")

    for op_name in ["GoldsteinPhaseFiltering", "GoldsteinPhaseFilter", "GoldsteinFilter"]:
        try:
            filtered = GPF.createProduct(op_name, filter_params, deburst)
            print(f"  Goldstein 滤波完成 (算子: {op_name})")
            break
        except Exception:
            continue
    else:
        print("  [警告] Goldstein 滤波不可用")
        print("  [提示] 请通过 SNAP Desktop → Tools → Plugin Manager 更新 S1-InSAR 插件")

    # ========== Step 7: 地形校正（地理编码，可选）==========
    print("[InSAR] Step 7/7: 地形校正（Terrain-Correction）...")
    try:
        tc_params = HashMap()
        tc_params.put("demName", "SRTM 3Sec")
        tc_params.put("demResamplingMethod", "BILINEAR_INTERPOLATION")
        tc_params.put("imgResamplingMethod", "BILINEAR_INTERPOLATION")
        tc_params.put("pixelSpacingInMeter", "10.0")
        tc_params.put("mapProjection", "EPSG:4326")
        tc_params.put("nodataValueAtSea", "true")
        tc_params.put("maskOutAreaWithoutElevation", "true")
        result = GPF.createProduct("Terrain-Correction", tc_params, filtered)
        print("  地形校正完成")
    except Exception as e:
        print(f"  [警告] 地形校正失败: {e.__class__.__name__}")
        print("  [提示] 将输出斜距坐标系的干涉结果")
        result = filtered

    print("  InSAR 处理完成")

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
            print(f"[错误] 需要至少 2 幅 SLC 影像，当前找到 {len(slc_files)} 幅")
            print(f"  请先通过管线下载 SLC 影像：")
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
