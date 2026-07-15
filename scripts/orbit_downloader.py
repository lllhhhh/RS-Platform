"""
orbit_downloader.py - Sentinel-1 轨道文件自动下载

功能：
自动下载 Sentinel-1 精密轨道文件（POEORB），供 snappy 预处理和 InSAR 使用。

轨道文件存储位置：
  Windows: C:\Users\<用户名>\.snap\auxdata\Orbits\Sentinel-1\POEORB\S1A\{year}\{month}\

使用方式：
    from scripts.orbit_downloader import ensure_orbit_files
    ensure_orbit_files(['2024-01-06', '2024-02-28'])
"""

import sys
import zipfile
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ESA 轨道文件服务器
ESA_ORBIT_BASE = "https://step.esa.int/auxdata/orbits/Sentinel-1/POEORB"

# SNAP 轨道文件本地目录
SNAP_AUXDATA_DIR = Path.home() / ".snap" / "auxdata" / "Orbits" / "Sentinel-1" / "POEORB"


def _get_orbit_dir(platform: str = "S1A", year: int = 2024, month: int = 1) -> Path:
    """获取轨道文件本地目录。"""
    return SNAP_AUXDATA_DIR / platform / f"{year}" / f"{month:02d}"


def _orbit_file_covers_date(orbit_file: Path, target_date: str) -> bool:
    """
    检查轨道文件是否覆盖指定日期。

    轨道文件名格式: S1A_OPER_AUX_POEORB_OPOD_{delivery}_V{start}_{stop}.EOF
    其中 start/stop 是轨道覆盖的起止时间。
    """
    name = orbit_file.name
    # 提取 V{start}_{stop} 部分
    parts = name.split("_V")
    if len(parts) < 2:
        return False

    try:
        date_part = parts[1].replace(".EOF", "")
        start_str, stop_str = date_part.split("_")
        start_date = datetime.strptime(start_str[:8], "%Y%m%d")
        stop_date = datetime.strptime(stop_str[:8], "%Y%m%d")
        target = datetime.strptime(target_date.replace("-", ""), "%Y%m%d")
        return start_date <= target <= stop_date
    except Exception:
        return False


def _find_existing_orbit(platform: str, target_date: str) -> Path:
    """查找已存在的轨道文件。"""
    try:
        dt = datetime.strptime(target_date.replace("-", ""), "%Y%m%d")
        orbit_dir = _get_orbit_dir(platform, dt.year, dt.month)
    except ValueError:
        return None

    if not orbit_dir.exists():
        return None

    for eof_file in orbit_dir.glob("*.EOF"):
        if _orbit_file_covers_date(eof_file, target_date):
            return eof_file

    return None


def _list_remote_orbit_files(platform: str, year: int, month: int) -> list:
    """
    列出远程轨道文件服务器上的可用文件。

    Returns:
        list: 文件名列表
    """
    url = f"{ESA_ORBIT_BASE}/{platform}/{year}/{month:02d}/"
    try:
        r = requests.get(url, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            return []

        # 解析 HTML 目录列表
        files = []
        for line in r.text.split("\n"):
            if ".EOF.zip" in line and platform in line:
                # 提取文件名
                start = line.find('href="')
                if start >= 0:
                    end = line.find('"', start + 6)
                    if end >= 0:
                        files.append(line[start + 6:end])
        return files
    except Exception:
        return []


def _download_orbit_file(platform: str, year: int, month: int, filename: str) -> Path:
    """
    下载并解压轨道文件。

    Args:
        platform: 平台（S1A 或 S1B）
        year: 年份
        month: 月份
        filename: 远程文件名（.EOF.zip）

    Returns:
        Path: 解压后的 .EOF 文件路径
    """
    orbit_dir = _get_orbit_dir(platform, year, month)
    orbit_dir.mkdir(parents=True, exist_ok=True)

    eof_name = filename.replace(".zip", "")
    eof_path = orbit_dir / eof_name

    if eof_path.exists():
        return eof_path

    url = f"{ESA_ORBIT_BASE}/{platform}/{year}/{month:02d}/{filename}"
    print(f"  下载: {filename}")

    try:
        r = requests.get(url, timeout=120, allow_redirects=True, stream=True)
        if r.status_code != 200:
            print(f"  [错误] 下载失败: HTTP {r.status_code}")
            return None

        zip_path = orbit_dir / filename
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        # 解压
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(orbit_dir)

        # 删除 zip
        zip_path.unlink()

        if eof_path.exists():
            print(f"  [完成] {eof_name}")
            return eof_path
        else:
            print(f"  [错误] 解压后未找到 {eof_name}")
            return None

    except Exception as e:
        print(f"  [错误] 下载失败: {e}")
        return None


def _find_and_download_orbit(platform: str, target_date: str) -> Path:
    """
    查找或下载覆盖指定日期的轨道文件。

    Args:
        platform: 平台（S1A 或 S1B）
        target_date: 目标日期（YYYY-MM-DD 或 YYYYMMDD）

    Returns:
        Path: 轨道文件路径
    """
    # 先检查本地
    existing = _find_existing_orbit(platform, target_date)
    if existing:
        return existing

    # 解析日期
    try:
        dt = datetime.strptime(target_date.replace("-", ""), "%Y%m%d")
    except ValueError:
        print(f"  [错误] 无效日期格式: {target_date}")
        return None

    print(f"[轨道文件] 未找到 {platform} {target_date} 的轨道文件，尝试下载...")

    # 列出远程文件
    remote_files = _list_remote_orbit_files(platform, dt.year, dt.month)
    if not remote_files:
        print(f"  [错误] 无法获取远程文件列表")
        return None

    # 查找覆盖目标日期的文件
    for filename in remote_files:
        if target_date.replace("-", "") in filename or _orbit_file_covers_date_name(filename, target_date):
            return _download_orbit_file(platform, dt.year, dt.month, filename)

    # 如果精确匹配失败，尝试下载所有文件（数量不多）
    print(f"  [提示] 精确匹配失败，尝试逐个检查...")
    for filename in remote_files:
        result = _download_orbit_file(platform, dt.year, dt.month, filename)
        if result and _orbit_file_covers_date(result, target_date):
            return result

    print(f"  [错误] 未找到覆盖 {target_date} 的轨道文件")
    return None


def _orbit_file_covers_date_name(filename: str, target_date: str) -> bool:
    """从文件名检查是否覆盖目标日期。"""
    try:
        # 提取日期部分
        parts = filename.split("_V")
        if len(parts) < 2:
            return False
        date_part = parts[1].replace(".EOF.zip", "").replace(".EOF", "")
        start_str, stop_str = date_part.split("_")
        start_date = datetime.strptime(start_str[:8], "%Y%m%d")
        stop_date = datetime.strptime(stop_str[:8], "%Y%m%d")
        target = datetime.strptime(target_date.replace("-", ""), "%Y%m%d")
        return start_date <= target <= stop_date
    except Exception:
        return False


def ensure_orbit_files(dates: list, platform: str = "S1A") -> dict:
    """
    确保指定日期的轨道文件可用，不存在则自动下载。

    Args:
        dates: 日期列表（YYYY-MM-DD 或 YYYYMMDD 格式）
        platform: 平台（S1A 或 S1B）

    Returns:
        dict: {日期: 轨道文件路径} 映射
    """
    results = {}
    for date in dates:
        orbit_path = _find_and_download_orbit(platform, date)
        if orbit_path:
            results[date] = orbit_path
        else:
            print(f"[警告] 无法获取 {date} 的轨道文件")
    return results


def get_dates_from_scenes(scenes: list) -> list:
    """
    从场景列表中提取唯一日期。

    Args:
        scenes: 场景信息列表（dict，含 date 或 date_display 字段）

    Returns:
        list: 唯一日期列表
    """
    dates = set()
    for scene in scenes:
        date = scene.get("date") or scene.get("date_display", "")
        if date:
            dates.add(date.replace("-", "")[:8])
    return sorted(dates)


def main():
    """独立运行：下载指定日期的轨道文件。"""
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel-1 轨道文件下载工具")
    parser.add_argument("dates", nargs="+", help="日期列表（YYYY-MM-DD 格式）")
    parser.add_argument("--platform", default="S1A", choices=["S1A", "S1B"], help="卫星平台")

    args = parser.parse_args()

    print("=" * 60)
    print("Sentinel-1 轨道文件下载")
    print("=" * 60)

    results = ensure_orbit_files(args.dates, args.platform)

    if results:
        print(f"\n成功获取 {len(results)} 个轨道文件:")
        for date, path in results.items():
            print(f"  {date}: {path}")
    else:
        print("\n未能获取任何轨道文件")


if __name__ == "__main__":
    main()
