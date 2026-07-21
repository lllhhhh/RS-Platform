# RS-Platform 遥感影像处理系统

基于 Sentinel-1/2 卫星数据的遥感影像下载、处理与展示平台。支持从 Microsoft Planetary Computer 批量获取数据，自动完成波段合成、去云/预处理、多景拼接、格式转换等处理流程，并通过 Web 服务提供可视化展示。

## 功能特性

| 功能 | 说明 |
|------|------|
| **多卫星支持** | Sentinel-2 L2A（光学）、Sentinel-1 GRD/SLC（SAR） |
| **STAC 搜索** | 按区域、日期搜索影像，S2 支持云量过滤，显示重叠率/覆盖率 |
| **波段选择** | S2 支持交互式选择波段或使用预设组合（rgb/false_color/vegetation 等） |
| **任务隔离** | 每次运行自动创建独立任务目录，支持任务管理和历史查询 |
| **研究区输入** | 支持 bbox 矩形、SHP 文件、行政区划名称/adcode 四种方式 |
| **批量下载** | S2/GRD 使用 ARIA2 并行下载，SLC 使用 CDSE 直接下载 |
| **波段合成** | S2: 用户选择的波段组合，S1: VV+VH→双通道 |
| **S1 GRD 预处理** | sarsen 处理链（纯 Python）：轨道文件→地形校正→辐射校正→dB |
| **SCL 去云** | 使用场景分类层去除云、云阴影、卷云像素（仅 S2，需要 SCL 波段） |
| **InSAR 形变监测** | GMTSAR Docker 服务：配准→干涉→滤波→解缠→形变提取（mm） |
| **自动资源管理** | 轨道文件、DEM 数据自动下载和缓存 |

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
│   (sarsen/SCL)      (独立/拼接)  (格式转换)                   │
│                                                              │
│  InSAR 形变监测（GMTSAR Docker 服务）                         │
│  SLC×2 → 轨道→配准→干涉→滤波→解缠→形变提取 → 形变图(mm)     │
└──────────────────────────────────────────────────────────────┘
```

## 管线流程

完整管线根据卫星类型自动适配：

| 步骤 | 脚本 | 说明 | S1 GRD | S1 SLC | S2 |
|------|------|------|:------:|:------:|:--:|
| 1 | `01_search_and_sign.py` / `cdse_s1_slc.py` | 搜索 + 覆盖率/重叠率计算 | MPC STAC | **CDSE STAC** | MPC STAC |
| 2 | — | 交互式场景/升降轨选择 | ✓ | ✓ | ✓ |
| 3 | — | URL 签名 | ✓ | — | ✓ |
| 4 | `02_aria2_download.py` / `cdse_s1_slc.py` | 批量下载 | ARIA2 | **CDSE 直下** | ARIA2 |
| 5 | `03_band_merge.py` | 波段合成 | vv+vh | vv+vh(SAFE提取) | 用户选择的波段 |
| 6 | `08_s1_preprocess.py` | sarsen 预处理（纯 Python） | **执行** | 跳过 | 跳过 |
| 7 | `04_cloud_mask.py` | SCL 去云 | 跳过 | 跳过 | **执行** |
| 8 | `07_mosaic_clip.py` | 裁剪（独立/拼接） | ✓ | 跳过 | ✓ |
| 9 | `05_tif_to_zarr.py` | TIF→ZARR | ✓ | 跳过 | ✓ |

**说明：**
- **S1 SLC**：下载后用于 InSAR 分析，跳过裁剪和 ZARR 转换
- **S1 GRD**：完整的 9 步处理流程
- **S2**：完整的 9 步处理流程，支持波段选择

### 任务隔离

每次运行管线自动创建独立任务目录：

```
data/tasks/
├── 20240115_120000_S1_SLC/
│   ├── downloads/
│   ├── merged/
│   ├── cloud_masked/
│   ├── mosaicked/
│   ├── zarr/
│   ├── insar/
│   └── task_info.json
├── 20240116_093000_S2/
└── task_latest -> ...  # 软链接指向最新任务
```

## 快速开始

### 1. 环境准备

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 启动 GMTSAR Docker 容器（InSAR 处理需要）
docker-compose up -d gmtsar

# 检查服务健康状态
curl http://localhost:8001/health
```

**说明：**
- **GRD 预处理**：使用 sarsen 库（纯 Python），无需额外安装
- **InSAR 处理**：使用 GMTSAR Docker 容器，需要 Docker 环境
- **DEM 数据**：通过 PyGMTSAR 自动从 AWS 下载，存储在 `data/dem/` 目录

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

### 2. 运行管线

```bash
# ==================== Sentinel-2（光学影像）====================
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0
python scripts/06_pipeline.py --adcode 110000
python scripts/06_pipeline.py --admin-name "北京市"
python scripts/06_pipeline.py --auto-select

# S2 波段选择
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0 --bands false_color
python scripts/06_pipeline.py --bbox 116.0 39.0 117.0 40.0 --bands B02 B03 B04 B08 SCL

# ==================== Sentinel-1 GRD（SAR 影像）====================
python scripts/06_pipeline.py --satellite sentinel1 --bbox 116.0 39.0 117.0 40.0

# ==================== Sentinel-1 SLC（用于 InSAR）====================
# 步骤1: 下载 SLC 数据
python scripts/06_pipeline.py --satellite sentinel1 --s1-product slc --bbox 116.0 39.0 117.0 40.0 --date "2024-01-01/2024-03-01"

# 步骤2: 执行 InSAR 分析
python scripts/09_insar_analysis.py --data-dir ./data/tasks/<任务ID> --polarization vv

# ==================== 任务管理 ====================
python scripts/task_manager.py list
python scripts/task_manager.py info TASK_ID
python scripts/task_manager.py cleanup --keep 5
```

### 3. 启动后端 API

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问 http://localhost:8000/docs 查看 API 文档。

## S2 波段选择

| 预设名称 | 波段组合 | 应用场景 |
|---------|---------|---------|
| `rgb` | B02, B03, B04 | 真彩色可视化 |
| `rgb_scl` | B02, B03, B04, SCL | 真彩色 + 去云（默认） |
| `false_color` | B08, B04, B03 | 假彩色（植被显示为红色） |
| `agriculture` | B11, B08, B02 | 农业监测 |
| `urban` | B12, B11, B04 | 城市环境 |
| `vegetation` | B08, B11, B02 | 植被健康分析 |
| `water` | B08, B11, B04 | 水体提取 |
| `all_10m` | B02, B03, B04, B08 | 所有10m波段 |

## InSAR 形变监测

独立模块 `09_insar_analysis.py`，通过 GMTSAR Docker 服务对两幅 Sentinel-1 SLC 影像执行 InSAR 处理。

### 处理链

```
扫描 SLC 场景 → 下载轨道文件 → 下载 DEM → 配准(Coregistration)
  → 干涉图生成(含 DEM 去地形) → Goldstein 滤波 → 相位解缠(Snaphu)
  → 形变提取(mm) → 导出 GeoTIFF
```

### 输出产品

| 文件 | 说明 |
|------|------|
| `phase.tif` | 干涉相位图 |
| `correlation.tif` | 相干性图 |
| `phase_filtered.tif` | Goldstein 滤波后相位 |
| `unwrapped_phase.tif` | 解缠相位图 |
| `deformation_los_mm.tif` | LOS 方向形变图（单位：mm） |

### 注意事项

- **Docker 服务**：InSAR 处理需要 GMTSAR Docker 容器运行，启动命令：`docker-compose up -d gmtsar`
- SLC 数据是斜距坐标，下载后**跳过裁剪和 ZARR 转换**
- 轨道文件和 DEM 数据会自动下载（首次运行需要网络）
- 需要至少两幅 SLC 影像（主影像和从影像）
- 建议选择时间跨度适中的影像对（太短无形变，太长失相干）
- 处理时间较长（通常 10-30 分钟），API 超时设置为 1 小时

## API 接口

### 影像 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/imagery/list` | 列出所有可用影像 |
| `GET` | `/api/imagery/{id}` | 获取影像详情 |
| `GET` | `/api/imagery/{id}/tile` | 获取影像切片（PNG） |
| `GET` | `/api/imagery/{id}/overview` | 获取影像概览图 |
| `POST` | `/api/imagery/download` | 触发新影像下载任务 |

### InSAR API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/insar/health` | 检查 GMTSAR 服务健康状态 |
| `POST` | `/api/insar/download` | 下载 SLC 数据（异步） |
| `POST` | `/api/insar/process` | 执行 InSAR 处理（异步） |
| `GET` | `/api/insar/tasks` | 列出所有 InSAR 任务 |
| `GET` | `/api/insar/tasks/{task_id}` | 获取任务详情和结果 |

#### SLC 数据下载请求示例

```bash
# 下载 SLC 数据
curl -X POST http://localhost:8000/api/insar/download \
  -H "Content-Type: application/json" \
  -d '{
    "bbox": [116.0, 39.0, 117.0, 40.0],
    "date_range": "2024-01-01/2024-03-01",
    "orbit_direction": "ASCENDING"
  }'
```

#### InSAR 处理请求示例

```bash
# 方式 1：指定 SLC 目录
curl -X POST http://localhost:8000/api/insar/process \
  -H "Content-Type: application/json" \
  -d '{
    "slc_dir": "./data/tasks/xxx/downloads/s1_slc",
    "polarization": "vv"
  }'

# 方式 2：指定数据目录
curl -X POST http://localhost:8000/api/insar/process \
  -H "Content-Type: application/json" \
  -d '{
    "data_dir": "./data/tasks/xxx",
    "polarization": "vv"
  }'

# 方式 3：直接指定主从影像
curl -X POST http://localhost:8000/api/insar/process \
  -H "Content-Type: application/json" \
  -d '{
    "master": "./data/tasks/xxx/downloads/s1_slc/S1A_...SAFE",
    "slave": "./data/tasks/xxx/downloads/s1_slc/S1A_...SAFE",
    "polarization": "vv"
  }'

# 查询任务状态
curl http://localhost:8000/api/insar/tasks/insar_20240115_120000
```

## 目录结构

```
RS-Platform/
├── config/
│   └── settings.py              # 全局配置（波段定义、预设组合等）
├── utils/
│   ├── coverage.py              # 覆盖率计算 + 交互式场景选择
│   └── datav_boundary.py        # DataV 行政区划边界获取
├── scripts/
│   ├── 01_search_and_sign.py    # STAC 搜索 + URL 签名 + 波段选择
│   ├── 02_aria2_download.py     # ARIA2 批量下载 + Token 刷新
│   ├── 03_band_merge.py         # 波段合成（S2: RGB, S1: VV+VH）
│   ├── 04_cloud_mask.py         # SCL 去云（仅 S2）
│   ├── 05_tif_to_zarr.py        # TIF → ZARR 转换
│   ├── 06_pipeline.py           # 整合管线（S1/S2 自适应）
│   ├── 07_mosaic_clip.py        # 多景裁剪（独立/拼接）
│   ├── 08_s1_preprocess.py      # S1 GRD sarsen 预处理（纯 Python）
│   ├── 09_insar_analysis.py     # InSAR 形变监测（GMTSAR Docker）
│   ├── dem_downloader.py        # DEM 数据下载（PyGMTSAR）
│   ├── insar_client.py          # InSAR 客户端（HTTP API）
│   ├── s1_preprocess_python.py  # GRD 预处理纯 Python 实现
│   ├── cdse_s1_slc.py           # CDSE S1 SLC 搜索下载
│   ├── orbit_downloader.py      # 轨道文件自动下载
│   └── task_manager.py          # 任务管理工具
├── docker/
│   └── gmtsar/
│       ├── Dockerfile           # GMTSAR 容器配置
│       ├── requirements.txt     # 容器内 Python 依赖
│       └── app.py               # 容器内 FastAPI 服务
├── docker-compose.yml           # Docker Compose 配置
├── backend/
│   ├── main.py                  # FastAPI 入口
│   ├── routers/imagery.py       # 影像 API 路由
│   ├── services/zarr_service.py # ZARR 数据服务
│   └── models/imagery.py        # 数据模型
├── data/
│   ├── tasks/                   # 任务目录（每次运行自动创建）
│   ├── dem/                     # DEM 数据缓存
│   └── boundaries/              # 行政区划 SHP 缓存
├── requirements.txt
└── README.md
```

## GMTSAR Docker 服务

InSAR 处理通过 GMTSAR Docker 容器执行，主应用通过 HTTP API 调用。

### 启动服务

```bash
# 构建并启动容器
docker-compose up -d gmtsar

# 检查容器状态
docker-compose ps

# 查看容器日志
docker-compose logs gmtsar

# 检查服务健康状态
curl http://localhost:8001/health
```

### API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `POST` | `/insar/process` | 执行 InSAR 处理 |

### 请求示例

```bash
curl -X POST http://localhost:8001/insar/process \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "test_001",
    "master_path": "tasks/xxx/downloads/s1_slc/S1A_...SAFE",
    "slave_path": "tasks/xxx/downloads/s1_slc/S1A_...SAFE",
    "polarization": "vv",
    "subswath": 2
  }'
```

### 注意事项

- Docker 容器需要 8GB+ 内存
- 首次运行会自动下载 GMTSAR 和 PyGMTSAR
- 处理时间较长（通常 10-30 分分钟）
- 主应用和容器通过共享卷（data/）传输数据

## DEM 数据管理

DEM 数据通过 PyGMTSAR 的 Tiles 类从 AWS 下载，支持以下数据源：

| DEM | 分辨率 | 来源 | 说明 |
|-----|--------|------|------|
| Copernicus GLO-30 | 30m | ESA | 全球覆盖，质量更高（推荐） |
| Copernicus GLO-90 | 90m | ESA | 全球覆盖 |
| SRTM 1Sec | 30m | NASA | 全球覆盖（南纬60°-北纬60°） |
| SRTM 3Sec | 90m | NASA | 同上 |

### 下载 DEM

```bash
# 为指定区域下载 DEM
python scripts/dem_downloader.py --bbox 116.0 39.0 117.0 40.0

# 为 SLC 场景下载 DEM
python scripts/dem_downloader.py --slc-dir data/tasks/xxx/downloads/s1_slc

# 列出已下载的 DEM
python scripts/dem_downloader.py --list
```

## 数据来源

| 数据 | 来源 | 说明 |
|------|------|------|
| Sentinel-2 L2A | [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) | 光学影像，大气校正后 |
| Sentinel-1 GRD | Microsoft Planetary Computer | SAR 影像，地距检测 |
| Sentinel-1 SLC | [Copernicus Data Space](https://dataspace.copernicus.eu/) | SAR 影像，单视复数（用于 InSAR） |
| 行政区划边界 | [阿里云 DataV](https://datav.aliyun.com/) GeoJSON API | 免费，自动缓存 |
| 轨道文件 | [ESA STEP](https://step.esa.int/) / AWS | 精密轨道文件，自动下载 |
| DEM 数据 | [AWS](https://registry.opendata.aws/) | Copernicus GLO-30 / SRTM，自动下载 |

## 关键依赖

| 包名 | 用途 |
|------|------|
| `pystac-client` | STAC API 客户端，搜索 Sentinel-1/2 数据 |
| `planetary-computer` | MPC URL 签名 |
| `rasterio` | GeoTIFF 读写、影像拼接、裁剪、重投影 |
| `sarsen` | SAR 处理库（纯 Python），GRD 地形校正和辐射校正 |
| `xarray-sentinel` | Sentinel-1 数据读取（xarray 扩展） |
| `pygmtsar` | DEM 下载（Tiles 类） |
| `xarray` / `zarr` | 多维数组处理，ZARR 格式支持 |
| `shapely` / `geopandas` | 几何对象处理，覆盖率计算 |
| `fastapi` / `uvicorn` | Web API 框架 |

**Docker 容器内依赖：**

| 包名 | 用途 |
|------|------|
| `pygmtsar` | InSAR 处理（需要 GMTSAR 二进制） |
| `fastapi` / `uvicorn` | 容器内 API 服务 |

## 许可证

本项目仅供学习和研究使用。Sentinel-1/2 数据由 ESA 提供，遵循 [Copernicus Open Access Hub](https://scihub.copernicus.eu/) 的数据政策。
