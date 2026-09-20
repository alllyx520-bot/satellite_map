FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=/tmp/matplotlib
RUN pip install --no-cache-dir numpy scipy rasterio pyproj shapely pandas matplotlib \
 && useradd --create-home --uid 10001 analyst
USER analyst
