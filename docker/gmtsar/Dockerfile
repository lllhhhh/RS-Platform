FROM ubuntu:22.04

# 设置非交互模式
ENV DEBIAN_FRONTEND=noninteractive

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    build-essential \
    gfortran \
    libgmt-dev \
    libnetcdf-dev \
    libgdal-dev \
    libhdf5-dev \
    wget \
    git \
    autoconf \
    automake \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 安装 GMTSAR
RUN git clone https://github.com/gmtsar/gmtsar.git /opt/gmtsar && \
    cd /opt/gmtsar && \
    autoconf && \
    ./configure --with-gmt-config=/usr/lib/gmt-config && \
    make && \
    make install && \
    ldconfig

# 设置 GMTSAR 环境变量
ENV GMTSAR_HOME=/opt/gmtsar
ENV PATH="${GMTSAR_HOME}/bin:${PATH}"

# 创建应用目录
WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# 复制应用代码
COPY app.py /app/app.py

# 暴露端口
EXPOSE 8001

# 启动 FastAPI 服务
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
