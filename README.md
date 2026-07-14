# RS-Platform 遥感影像处理系统

基于 Sentinel-1/2 卫星数据的遥感影像下载、处理与展示平台。支持从 Microsoft Planetary Computer 批量获取数据，自动完成波段合成、去云/预处理、多景拼接、格式转换等处理流程，并通过 Web 服务提供可视化展示。

## 功能特性

| 功能 | 说明 |
|------|------|
| **多卫星支持** | Sentinel-2 L2A（光学）、Sentinel-1 GRD/SLC（SAR） |
| **STAC 搜索** | 按区域、日期搜索影像，S2 支持云量过滤 |
| **覆盖率计算** | 计算每景影像对研究区的覆盖率，支持单景/多景分析 |
| **交互式场景选择** | 展示各时相覆盖率/云量，用户手动选择下载目标 |
| **研究区输入** | 支持 bbox 矩形、SHP 文件、行政区划名称/adcode 四种方式 |
| **批量下载** | 使用 ARIA2 多连接并行下载，支持断点续传，Token 自动刷新 |
| **波段合成** | S2: B02+B03+B04→RGB，S1: VV+VH→双通道 |
| **S1 GRD 预处理** | snappy 处理链：轨道文件→辐射定标→斑点滤波→地形校正→dB |
| **SCL 去云** | 使用场景分类层去除云、云阴影、卷云像素（仅 S2） |
| **独立裁剪** | 多景各自全覆盖时独立裁剪，否则拼接后裁剪 |
| **InSAR 形变监测** | 两幅 SLC 干涉处理，输出形变图、相干性图和分析报告 |
| **ZARR 转换** | TIF→ZARR 分块存储，前端按需加载 |
| **REST API** | FastAPI 后端，支持 S1/S2 影像下载触发 |

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    前端 (React + OpenLayers)                 │
│              地图展示 / 参数配置 / 影像浏览                    │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────┐
│                    后端 (FastAPI)                            │
│           REST API / ZARR 数据服务 / 任务调度                 │
└───────┬──────────────────────────────────────┬──────────────┘
        │                                      │
┌───────▼──────────┐                ┌──────────▼──────────────┐
│ PostgreSQL +     │                │  ZARR 数据存储           │
│ PostGIS          │                │  (分块压缩数组)          │
│ (元数据管理)      │                │                          │
└──────────────────┘                └──────────────────────────┘
                                            ▲
┌───────────────────────────────────────────┴──────────────────┐
│                   数据处理管线                                │
│                                                              │
│  MPC STAC / CDSE STAC ──→ 场景选择 ──→ ARIA2/CDSE 下载 ──→ 波段合成  │
│  (S2+S1 GRD / S1 SLC)   (交互/自动)     (批量+Token)    (RGB/双通道)   │
│                                                              │
│  ──→ S1预处理/S2去云 ──→ 裁剪 ──→ ZARR                      │
│     (snappy/SCL)     (独立/拼接)  (格式转换)                  │
│                                                              │
│  InSAR 形变监测（独立模块）                                    │
│  SLC×2 → 轨道→配准→干涉→滤波→地形校正 → 形变图               │
└──────────────────────────────────────────────────────────────┘
```

## 管线流程

完整管线包含 9 个步骤，根据卫星类型自动适配：

| 步骤 | 脚本 | 说明 | S1 GRD | S1 SLC | S2 |
|------|------|------|:------:|:------:|:--:|
| 1 | `01_search_and_sign.py` / `eodag_s1_slc.py` | STAC / CDSE 搜索 + 覆盖率计算 | MPC STAC | **CDSE STAC** | MPC STAC |
| 2 | — | 交互式场景/升降轨选择 | ✓ | ✓ | ✓ |
| 3 | — | URL 签名 | ✓ | — | ✓ |
| 4 | `02_aria2_download.py` / `eodag_s1_slc.py` | 批量下载 | ARIA2 | **CDSE 直下** | ARIA2 |
| 5 | `03_band_merge.py` | 波段合成 | vv+vh | vv+vh(SAFE提取) | B02+B03+B04 |
| 6 | `08_s1_preprocess.py` | snappy 预处理 | **执行** | 跳过 | 跳过 |
| 7 | `04_cloud_mask.py` | SCL 去云 | 跳过 | 跳过 | **执行** |
| 8 | `07_mosaic_clip.py` | 裁剪（独立/拼接） | ✓ | ✓ | ✓ |
| 9 | `05_tif_to_zarr.py` | TIF→ZARR | ✓ | ✓ | ✓ |

**InSAR 形变监测**（`09_insar_analysis.py`）为独立模块，不集成到常规管线。

## 快速开始

### 1. 环境准备

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 安装 esa_snappy（S1 预处理和 InSAR 需要）：
# 1. 安装 ESA SNAP Desktop: https://step.esa.int/
# 2. pip install esa-snappy
# 3. 解压 jpy 模块（如报错 jpyutil）：
#    python -c "import zipfile,glob;whl=glob.glob(r'<site-packages>/esa_snappy/lib/jpy*win*whl')[0];z=zipfile.ZipFile(whl);[z.extract(n,'<site-packages>/esa_snappy') for n in z.namelist() if n.endswith('.py') or n.endswith('.pyd')]"
# 4. 设置环境变量 JAVA_HOME=<SNAP安装路径>/jre
```

**Copernicus Data Space 认证（S1 SLC 下载需要）：**

S1 SLC 通过 [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/) 下载，需要免费注册账号。

```bash
# 方式一：保存认证信息到配置文件（推荐）
python -c "
import json, os
config_dir = os.path.expanduser('~/.rs_platform')
os.makedirs(config_dir, exist_ok=True)
json.dump({'username': 'your_email', 'password': 'your_password'}, open(os.path.join(config_dir, 'cdse_config.json'), 'w'), indent=2)
"

# 方式二：设置环境变量
export CDSE_USERNAME=your_email
export CDSE_PASSWORD=your_password
```

Token 首次认证后自动缓存，无需每次输入密码。

### 2. 运行管线

```bash
# ==================== Sentinel-2（光学影像）====================
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0
python scripts/06_pipeline.py --adcode 110000                   # 行政区划
python scripts/06_pipeline.py --admin-name "北京市"              # 模糊搜索
python scripts/06_pipeline.py --auto-select                     # 自动选择最优

# ==================== Sentinel-1 GRD（SAR 影像）====================
python scripts/06_pipeline.py --satellite sentinel1 --bbox 116.0 39.0 117.0 40.0
python scripts/06_pipeline.py --satellite sentinel1 --adcode 110000

# ==================== Sentinel-1 SLC（用于 InSAR）====================
python scripts/06_pipeline.py --satellite sentinel1 --s1-product slc --bbox 116.0 39.0 117.0 40.0 --date "2024-01-01/2024-03-01"

# ==================== InSAR 形变监测 ====================
python scripts/09_insar_analysis.py --data-dir ./data --polarization vv
python scripts/09_insar_analysis.py --master path/master.tif --slave path/slave.tif --polarization vv
```

### 3. 启动后端 API

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问 http://localhost:8000/docs 查看 API 文档。

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/imagery/list` | 列出所有可用影像 |
| `GET` | `/api/imagery/{id}` | 获取影像详情 |
| `GET` | `/api/imagery/{id}/tile` | 获取影像切片（PNG） |
| `GET` | `/api/imagery/{id}/overview` | 获取影像概览图 |
| `POST` | `/api/imagery/download` | 触发新影像下载任务 |

**下载请求示例：**

```json
// Sentinel-2
{"bbox": [116.0, 39.0, 117.0, 40.0], "date_range": "2024-01-01/2024-06-30", "cloud_cover_max": 20}

// Sentinel-1 GRD
{"adcode": "110000", "date_range": "2024-01-01/2024-06-30", "satellite": "sentinel1"}

// Sentinel-1 SLC
{"bbox": [116.0, 39.0, 117.0, 40.0], "date_range": "2024-01-01/2024-03-01", "satellite": "sentinel1", "s1_product": "slc"}
```

## 目录结构

```
RS-Platform/
├── config/
│   └── settings.py              # 全局配置（S1/S2 集合名、波段定义等）
├── utils/
│   ├── coverage.py              # 覆盖率计算 + 交互式场景选择
│   └── datav_boundary.py        # DataV 行政区划边界获取
├── scripts/
│   ├── 01_search_and_sign.py    # STAC 搜索（S1/S2）+ URL 签名
│   ├── 02_aria2_download.py     # ARIA2 批量下载 + Token 刷新
│   ├── 03_band_merge.py         # 波段合成（S2: RGB, S1: VV+VH）
│   ├── 04_cloud_mask.py         # SCL 去云（仅 S2）
│   ├── 05_tif_to_zarr.py        # TIF → ZARR 转换
│   ├── 06_pipeline.py           # 整合管线（9 步，S1/S2 自适应）
│   ├── 07_mosaic_clip.py        # 多景裁剪（独立/拼接）
│   ├── 08_s1_preprocess.py      # S1 GRD snappy 预处理
│   ├── 09_insar_analysis.py     # InSAR 形变监测
│   └── eodag_s1_slc.py          # CDSE S1 SLC 搜索下载
├── backend/
│   ├── main.py                  # FastAPI 入口
│   ├── routers/imagery.py       # 影像 API 路由
│   ├── services/zarr_service.py # ZARR 数据服务
│   └── models/imagery.py        # 数据模型
├── data/
│   ├── downloads/               # ARIA2 下载的原始波段
│   ├── merged/                  # 合成后 TIF（S2: RGB, S1: VV+VH）
│   ├── cloud_masked/            # 去云/S1预处理后的 TIF
│   ├── mosaicked/               # 裁剪后的 TIF
│   ├── zarr/                    # ZARR 输出
│   ├── insar/                   # InSAR 输出（形变图、相干性图、报告）
│   └── boundaries/              # 行政区划 SHP 缓存
├── requirements.txt
└── README.md
```

## 数据来源

| 数据 | 来源 | 说明 |
|------|------|------|
| Sentinel-2 L2A | [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) | 光学影像，大气校正后 |
| Sentinel-1 GRD | Microsoft Planetary Computer | SAR 影像，地距检测 |
| Sentinel-1 SLC | [Copernicus Data Space](https://dataspace.copernicus.eu/) | SAR 影像，单视复数（用于 InSAR），OAuth2 认证 |
| 行政区划边界 | [阿里云 DataV](https://datav.aliyun.com/) GeoJSON API | 免费，自动缓存 |

## 关键依赖

| 包名 | 用途 |
|------|------|
| `pystac-client` | STAC API 客户端，搜索 Sentinel-1/2 数据 |
| `planetary-computer` | MPC URL 签名，获取带 SAS Token 的下载链接 |
| `rasterio` | GeoTIFF 读写、影像拼接、裁剪 |
| `esa_snappy` | ESA SNAP Python 绑定，S1 预处理和 InSAR 分析 |
| `xarray` / `zarr` | 多维数组处理，ZARR 格式支持 |
| `shapely` / `geopandas` | 几何对象处理，覆盖率计算，SHP 读取 |
| `fastapi` / `uvicorn` | Web API 框架 |

## InSAR 形变监测

独立模块 `09_insar_analysis.py`，对两幅 Sentinel-1 SLC 影像执行 InSAR 处理：

```
主影像 → Apply-Orbit-File ─┐
                           ├→ Back-Geocoding → Interferogram → TopoPhaseRemoval → GoldsteinFilter → Terrain-Correction
从影像 → Apply-Orbit-File ─┘
```

**输出产品：**
- 干涉相位图（`*_phase.tif`）
- 相干性图（`*_coherence.tif`）
- 形变图（`*_deformation.tif`）
- 分析报告（`*_report.json`：形变量统计、相干性统计）

## 后续开发

- [ ] React + OpenLayers 前端界面
- [ ] PostgreSQL + PostGIS 数据库集成
- [ ] SLC 完整 SAFE 格式下载（支持轨道文件自动应用）
- [ ] InSAR 相位解缠（SNAPHU 集成）
- [ ] 时序 InSAR 分析（SBAS/PS-InSAR）
- [ ] 变化检测算法集成
- [ ] 地物分类算法集成

## 许可证

本项目仅供学习和研究使用。Sentinel-1/2 数据由 ESA 提供，遵循 [Copernicus Open Access Hub](https://scihub.copernicus.eu/) 的数据政策。
