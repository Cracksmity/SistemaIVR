#!/usr/bin/env python3
"""
Índice de Viabilidad para Redensificación (IVR) — Guadalajara, Jalisco, México.

Combina AGEBs del Censo INEGI (Shapefile con POBTOT) con POIs de OpenStreetMap
(osmnx) para identificar polígonos con alta densidad de servicios y baja densidad
de población, normalizado en el rango [0, 1].
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import osmnx as ox
import pandas as pd
from shapely.geometry import MultiPolygon, Polygon
from sklearn.preprocessing import MinMaxScaler

# CRS métrico para Jalisco (UTM zona 13N)
METRIC_CRS = "EPSG:32613"
WGS84 = "EPSG:4326"

# Geocodificación del límite municipal
GUADALAJARA_QUERY = "Guadalajara, Jalisco, Mexico"

# Tags OSM: amenity (hospitales, escuelas, mercados) + infraestructura de transporte público
OSM_TAGS: dict[str, Any] = {
    "amenity": ["hospital", "school", "marketplace"],
    "public_transport": ["platform", "station", "stop_position"],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def load_agebs(shp_path: str | Path) -> gpd.GeoDataFrame:
    """
    Carga el Shapefile de AGEBs (INEGI / ITER) con geopandas.

    Valida existencia del .shp, presencia de geometrías y columna POBTOT.
    """
    path = Path(shp_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"No se encontró el Shapefile: {path}")

    try:
        gdf = gpd.read_file(path)
    except Exception as exc:
        raise RuntimeError(f"Error al leer el Shapefile: {exc}") from exc

    if gdf.empty:
        raise ValueError("El GeoDataFrame de AGEBs está vacío.")

    if "POBTOT" not in gdf.columns:
        raise ValueError(
            "Falta la columna 'POBTOT'. Verifica que el Shapefile del censo la incluya."
        )

    if gdf.geometry.isna().all():
        raise ValueError("Todas las geometrías de AGEB son nulas.")

    gdf = gdf.copy()
    gdf["POBTOT"] = pd.to_numeric(gdf["POBTOT"], errors="coerce").fillna(0.0)

    # Asegurar CRS definido (INEGI suele usar ITRF2008 / GRS80; si viene sin CRS, asumir WGS84)
    if gdf.crs is None:
        logger.warning("AGEBs sin CRS definido; asignando EPSG:4326.")
        gdf.set_crs(WGS84, inplace=True)

    return gdf


def download_guadalajara_boundary() -> gpd.GeoDataFrame:
    """
    Descarga el polígono delimitado de Guadalajara vía geocodificación Nominatim (osmnx).
    """
    try:
        boundary = ox.geocode_to_gdf(GUADALAJARA_QUERY)
    except Exception as exc:
        raise RuntimeError(
            "No se pudo obtener el límite de Guadalajara (geocode_to_gdf). "
            "Revisa conexión a internet o intenta más tarde."
        ) from exc

    if boundary is None or boundary.empty:
        raise RuntimeError("La geocodificación devolvió un resultado vacío.")

    if boundary.crs is None:
        boundary.set_crs(WGS84, inplace=True)

    return boundary


def _unified_boundary_geom(boundary_gdf: gpd.GeoDataFrame):
    """Unifica geometrías del límite (compatible con varias versiones de GeoPandas)."""
    gser = boundary_gdf.geometry
    if hasattr(gser, "union_all"):
        return gser.union_all()
    return boundary_gdf.unary_union


def _polygon_from_boundary(boundary_gdf: gpd.GeoDataFrame) -> Polygon | MultiPolygon:
    """Unifica geometrías del límite en un único (Multi)Polygon para consultas OSM."""
    geom = _unified_boundary_geom(boundary_gdf)
    if not isinstance(geom, (Polygon, MultiPolygon)):
        raise TypeError(f"Geometría de límite no soportada: {type(geom)}")
    return geom


def download_pois(
    boundary_gdf: gpd.GeoDataFrame,
    tags: dict[str, Any] | None = None,
) -> gpd.GeoDataFrame:
    """
    Descarga POIs de OpenStreetMap dentro del polígono de Guadalajara.

    Usa ox.features_from_polygon con los tags indicados (amenity + public_transport).
    """
    tags = tags or OSM_TAGS
    poly = _polygon_from_boundary(boundary_gdf)

    try:
        pois = ox.features_from_polygon(poly, tags)
    except Exception as exc:
        logger.warning(
            "Fallo al descargar POIs desde OSM: %s. Se continuará sin POIs (conteo 0).",
            exc,
        )
        return gpd.GeoDataFrame(geometry=[], crs=boundary_gdf.crs)

    if pois is None or pois.empty:
        logger.warning("OSM no devolvió POIs para los tags solicitados.")
        return gpd.GeoDataFrame(geometry=[], crs=boundary_gdf.crs)

    if pois.crs is None:
        pois.set_crs(WGS84, inplace=True)

    return pois


def geometries_to_points(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Convierte geometrías no puntuales a centroides para el cruce espacial tipo POI.
    """
    out = gdf.copy()
    geom_types = out.geometry.geom_type
    non_point = ~geom_types.isin(["Point", "MultiPoint"])
    if non_point.any():
        out.loc[non_point, "geometry"] = out.loc[non_point, "geometry"].centroid
    return out


def filter_agebs_intersecting_boundary(
    agebs: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Mantiene solo AGEBs que intersectan el límite municipal (análisis focalizado)."""
    b = boundary.to_crs(agebs.crs)
    union = _unified_boundary_geom(b)
    mask = agebs.geometry.intersects(union)
    n = int(mask.sum())
    logger.info("AGEBs que intersectan Guadalajara: %d de %d", n, len(agebs))
    return agebs.loc[mask].copy()


def count_pois_by_ageb(
    agebs_metric: gpd.GeoDataFrame,
    pois_metric: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """
    Cruce espacial: cuenta cuántos POIs caen *dentro* de cada polígono AGEB.

    - Se usa ``predicate='within'``: cada punto POI debe estar geométricamente
      dentro del polígono del AGEB (relación espacial estándar punto-en-polígono).
    - ``gpd.sjoin`` enlaza filas de POIs con AGEBs; luego se agrega por el índice
      del AGEB coincidente (``index_right``) para obtener conteos por polígono.
    """
    result = agebs_metric.copy()

    if pois_metric.empty or pois_metric.geometry.isna().all():
        result["pois_count"] = 0
        return result

    pois_pts = geometries_to_points(pois_metric)
    pois_pts = pois_pts[pois_pts.geometry.notna()].copy()

    # sjoin: izquierda POI, derecha AGEB; inner solo asigna POIs que caen dentro de algún AGEB
    joined = gpd.sjoin(
        pois_pts,
        result[["geometry"]],
        how="inner",
        predicate="within",
    )

    if joined.empty:
        result["pois_count"] = 0
        return result

    counts = joined.groupby("index_right").size()
    result["pois_count"] = result.index.map(counts).fillna(0).astype(int)
    return result


def compute_area_km2(gdf_metric: gpd.GeoDataFrame) -> pd.Series:
    """Área en km² a partir de geometrías en CRS métrico (metros)."""
    areas_m2 = gdf_metric.geometry.area
    return areas_m2 / 1_000_000.0


def _winsorize_upper(series: pd.Series, quantile: float) -> tuple[pd.Series, float]:
    """
    Recorta valores extremos por arriba al percentil dado (winsorización superior).

    Las densidades no negativas se dejan en [0, Q_q]; así los outliers no dominan
    el MinMaxScaler posterior.
    """
    s = pd.to_numeric(series, errors="coerce").fillna(0.0)
    cap = float(s.quantile(quantile))
    if not np.isfinite(cap) or cap <= 0:
        return s.copy(), cap if np.isfinite(cap) else float("nan")
    return s.clip(upper=cap), cap


def compute_densities_and_ivr(
    agebs_metric: gpd.GeoDataFrame,
    winsor_quantile: float = 0.98,
) -> gpd.GeoDataFrame:
    """
    Calcula densidad de población y de servicios (por km²), aplica winsorización
    superior antes de normalizar con Min-Max, y deriva IVR en [0, 1].

    Los valores crudos se conservan en ``dens_pob`` y ``dens_serv``; las versiones
    recortadas usadas para el índice van en ``dens_pob_winsor`` y ``dens_serv_winsor``.
    """
    if not 0.0 < winsor_quantile < 1.0:
        raise ValueError("winsor_quantile debe estar en (0, 1), por ejemplo 0.95 o 0.98.")

    gdf = agebs_metric.copy()
    gdf["area_km2"] = compute_area_km2(gdf)

    # Evitar división por cero o áreas degeneradas
    min_area = 1e-6
    safe_area = gdf["area_km2"].replace(0, np.nan).clip(lower=min_area)
    safe_area = safe_area.fillna(min_area)

    gdf["dens_pob"] = gdf["POBTOT"] / safe_area
    gdf["dens_serv"] = gdf["pois_count"] / safe_area

    gdf["dens_pob_winsor"], cap_pob = _winsorize_upper(gdf["dens_pob"], winsor_quantile)
    gdf["dens_serv_winsor"], cap_serv = _winsorize_upper(gdf["dens_serv"], winsor_quantile)
    logger.info(
        "Winsorización (q=%.4f): tope dens_pob=%.6f, tope dens_serv=%.6f (por km²).",
        winsor_quantile,
        cap_pob if np.isfinite(cap_pob) else float("nan"),
        cap_serv if np.isfinite(cap_serv) else float("nan"),
    )

    scaler_pob = MinMaxScaler()
    scaler_serv = MinMaxScaler()

    gdf["dens_pob_norm"] = scaler_pob.fit_transform(gdf[["dens_pob_winsor"]]).ravel()
    gdf["dens_serv_norm"] = scaler_serv.fit_transform(gdf[["dens_serv_winsor"]]).ravel()

    ivr_raw = gdf["dens_serv_norm"] - gdf["dens_pob_norm"]
    gdf["ivr_raw"] = ivr_raw

    lo, hi = float(ivr_raw.min()), float(ivr_raw.max())
    rng = hi - lo
    if not np.isfinite(rng) or rng <= 0:
        gdf["IVR"] = 0.5
        logger.warning("IVR raw sin rango (constante); asignando IVR=0.5 en todos los AGEBs.")
    else:
        gdf["IVR"] = (ivr_raw - lo) / rng

    return gdf


def build_interactive_map(
    agebs_with_ivr: gpd.GeoDataFrame,
    output_html: str | Path,
    boundary_wgs84: gpd.GeoDataFrame | None = None,
    map_scheme: str = "Quantiles",
    map_k: int = 10,
) -> None:
    """
    Mapa interactivo (folium vía geopandas.explore) coloreado por columna IVR.

    Usa clasificación por cuantiles (mapclassify) para evitar intervalos iguales
    lineales que aplastan el contraste cuando la distribución está sesgada.
    """
    if map_k < 2:
        logger.warning("map_k=%s inválido; usando k=10.", map_k)
        map_k = 10

    gdf = agebs_with_ivr.to_crs(WGS84)

    kwargs: dict[str, Any] = {
        "column": "IVR",
        "cmap": "YlOrRd",
        "legend": True,
        "tiles": "CartoDB positron",
        "style_kwds": {"stroke": True, "color": "#333333", "weight": 0.5},
        "scheme": map_scheme,
        "k": map_k,
        "legend_kwds": {"title": f"IVR ({map_scheme}, k={map_k})"},
    }

    tooltip_cols = [
        c
        for c in [
            "POBTOT",
            "pois_count",
            "dens_pob",
            "dens_serv",
            "dens_pob_winsor",
            "dens_serv_winsor",
            "IVR",
            "area_km2",
        ]
        if c in gdf.columns
    ]
    if tooltip_cols:
        kwargs["tooltip"] = tooltip_cols

    try:
        m = gdf.explore(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Mapa con scheme=%s k=%s no disponible (%s); usando escala por defecto.",
            map_scheme,
            map_k,
            exc,
        )
        kwargs.pop("scheme", None)
        kwargs.pop("k", None)
        kwargs.pop("legend_kwds", None)
        m = gdf.explore(**kwargs)

    if boundary_wgs84 is not None and not boundary_wgs84.empty:
        b = boundary_wgs84.to_crs(WGS84)
        b.explore(
            m=m,
            style_kwds={"fillOpacity": 0, "color": "#0066cc", "weight": 2},
        )

    out = Path(output_html)
    out.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(out))
    logger.info("Mapa guardado en: %s", out.resolve())


def run_pipeline(
    shp_path: str | Path,
    output_html: str | Path,
    output_csv: str | Path | None = None,
    output_gpkg: str | Path | None = None,
    skip_boundary_filter: bool = False,
    winsor_quantile: float = 0.98,
    map_scheme: str = "Quantiles",
    map_k: int = 10,
) -> gpd.GeoDataFrame:
    """Orquesta carga, OSM, métricas espaciales, IVR y exportación."""
    agebs = load_agebs(shp_path)
    logger.info("AGEBs cargados: %d registros", len(agebs))

    boundary = download_guadalajara_boundary()
    pois = download_pois(boundary)

    if not skip_boundary_filter:
        agebs = filter_agebs_intersecting_boundary(agebs, boundary)
        if agebs.empty:
            raise RuntimeError(
                "Ningún AGEB intersecta el límite de Guadalajara. "
                "Verifica CRS del Shapefile o usa --no-boundary-filter."
            )

    # Reproyectar a CRS métrico para áreas y densidades por km²
    agebs_m = agebs.to_crs(METRIC_CRS)
    boundary_m = boundary.to_crs(METRIC_CRS)
    poly_union = _polygon_from_boundary(boundary_m)

    pois_m = pois.to_crs(METRIC_CRS) if not pois.empty else pois
    if not pois_m.empty:
        # Recorte opcional de POIs al polígono municipal (evita ruido fuera del área)
        pois_m = pois_m[pois_m.geometry.intersects(poly_union)].copy()

    agebs_m = count_pois_by_ageb(agebs_m, pois_m)
    agebs_m = compute_densities_and_ivr(agebs_m, winsor_quantile=winsor_quantile)

    logger.info(
        "Resumen IVR: min=%.4f max=%.4f mean=%.4f | POIs usados (tras clip): %d",
        float(agebs_m["IVR"].min()),
        float(agebs_m["IVR"].max()),
        float(agebs_m["IVR"].mean()),
        int(pois_m.shape[0]) if not pois_m.empty else 0,
    )

    build_interactive_map(
        agebs_m,
        output_html,
        boundary_wgs84=boundary,
        map_scheme=map_scheme,
        map_k=map_k,
    )

    if output_csv:
        csv_path = Path(output_csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        drop_geom = agebs_m.drop(columns=["geometry"], errors="ignore")
        drop_geom.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info("Tabla exportada a CSV: %s", csv_path.resolve())

    if output_gpkg:
        gpkg_path = Path(output_gpkg)
        gpkg_path.parent.mkdir(parents=True, exist_ok=True)
        agebs_m.to_file(gpkg_path, driver="GPKG")
        logger.info("GeoDataFrame exportado a GPKG: %s", gpkg_path.resolve())

    return agebs_m


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Calcula el IVR (redensificación) para AGEBs en Guadalajara, México.",
    )
    p.add_argument(
        "--shp-path",
        required=True,
        type=str,
        help="Ruta al archivo .shp de AGEBs INEGI (con columna POBTOT).",
    )
    p.add_argument(
        "--output-html",
        type=str,
        default="mapa_ivr_guadalajara.html",
        help="Ruta del mapa interactivo HTML (folium).",
    )
    p.add_argument(
        "--output-csv",
        type=str,
        default=None,
        help="Opcional: exportar atributos tabulares a CSV.",
    )
    p.add_argument(
        "--output-gpkg",
        type=str,
        default=None,
        help="Opcional: exportar resultados con geometría a GeoPackage.",
    )
    p.add_argument(
        "--no-boundary-filter",
        action="store_true",
        help="No filtrar AGEBs por intersección con el límite de Guadalajara.",
    )
    p.add_argument(
        "--winsor-quantile",
        type=float,
        default=0.98,
        help="Percentil superior (0-1) para winsorizar densidades antes del Min-Max (ej. 0.95 o 0.98).",
    )
    p.add_argument(
        "--map-scheme",
        type=str,
        default="Quantiles",
        help="Esquema mapclassify para colores del mapa (por defecto Quantiles).",
    )
    p.add_argument(
        "--map-k",
        type=int,
        default=10,
        help="Número de clases en el mapa (ej. 10 = deciles con Quantiles).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_pipeline(
            shp_path=args.shp_path,
            output_html=args.output_html,
            output_csv=args.output_csv,
            output_gpkg=args.output_gpkg,
            skip_boundary_filter=args.no_boundary_filter,
            winsor_quantile=args.winsor_quantile,
            map_scheme=args.map_scheme,
            map_k=args.map_k,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error inesperado: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
