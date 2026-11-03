from dataclasses import dataclass
import json
import math
from typing import Any, Optional

EARTH_RADIUS_METERS = 6_371_000.0


@dataclass(frozen=True)
class GeofenceEvaluationResult:
    """Result of server-side geometric evaluation of a coordinate against a geofence."""

    inside: bool
    accuracy_valid: bool
    distance_meters: Optional[float]
    max_accuracy_meters: float
    reason_code: Optional[str] = None
    reason_message: Optional[str] = None


def haversine_distance_meters(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """Compute the great-circle distance in meters between two WGS84 coordinates."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return EARTH_RADIUS_METERS * c


def point_in_polygon(
    latitude: float,
    longitude: float,
    vertices: list[dict[str, float]],
) -> bool:
    """Determine if a point (latitude, longitude) is inside a polygon using the Ray-Casting algorithm."""
    if len(vertices) < 3:
        return False

    inside = False
    n = len(vertices)
    j = n - 1
    for i in range(n):
        yi = float(vertices[i]["latitude"])
        xi = float(vertices[i]["longitude"])
        yj = float(vertices[j]["latitude"])
        xj = float(vertices[j]["longitude"])

        intersects = ((yi > latitude) != (yj > latitude)) and (
            longitude < (xj - xi) * (latitude - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i

    return inside


def parse_polygon_vertices(vertices_json: Optional[str]) -> list[dict[str, float]]:
    """Parse JSON-encoded polygon vertices [{'latitude': ..., 'longitude': ...}, ...]."""
    if not vertices_json:
        return []
    try:
        raw: Any = json.loads(vertices_json)
        if not isinstance(raw, list):
            return []
        cleaned: list[dict[str, float]] = []
        for item in raw:
            if isinstance(item, dict) and "latitude" in item and "longitude" in item:
                cleaned.append(
                    {
                        "latitude": float(item["latitude"]),
                        "longitude": float(item["longitude"]),
                    }
                )
        return cleaned
    except Exception:
        return []


def evaluate_geofence(
    *,
    latitude: float,
    longitude: float,
    accuracy_meters: float,
    geofence_type: str,
    max_accuracy_meters: float = 50.0,
    center_lat: Optional[float] = None,
    center_lon: Optional[float] = None,
    radius_meters: Optional[float] = None,
    vertices: Optional[list[dict[str, float]]] = None,
) -> GeofenceEvaluationResult:
    """Server-side validation of GPS accuracy and radial/polygonal geofence containment."""
    eff_max_acc = max_accuracy_meters if max_accuracy_meters > 0 else 50.0

    if accuracy_meters < 0 or accuracy_meters > eff_max_acc:
        return GeofenceEvaluationResult(
            inside=False,
            accuracy_valid=False,
            distance_meters=None,
            max_accuracy_meters=eff_max_acc,
            reason_code="LOCATION_ACCURACY_TOO_LOW",
            reason_message=(
                f"La precisión de ubicación ({accuracy_meters:.1f} m) excede el máximo permitido "
                f"({eff_max_acc:.1f} m)."
            ),
        )

    g_type = (geofence_type or "radial").strip().lower()
    if g_type == "polygonal":
        poly = vertices or []
        is_inside = point_in_polygon(latitude, longitude, poly)
        dist = None
        if poly:
            dist = round(
                haversine_distance_meters(
                    latitude,
                    longitude,
                    poly[0]["latitude"],
                    poly[0]["longitude"],
                ),
                2,
            )
        if not is_inside:
            return GeofenceEvaluationResult(
                inside=False,
                accuracy_valid=True,
                distance_meters=dist,
                max_accuracy_meters=eff_max_acc,
                reason_code="OUTSIDE_GEOFENCE",
                reason_message="Tu ubicación actual se encuentra fuera del polígono autorizado.",
            )
        return GeofenceEvaluationResult(
            inside=True,
            accuracy_valid=True,
            distance_meters=dist,
            max_accuracy_meters=eff_max_acc,
        )

    # Radial geofence
    if center_lat is None or center_lon is None or radius_meters is None or radius_meters <= 0:
        return GeofenceEvaluationResult(
            inside=False,
            accuracy_valid=True,
            distance_meters=None,
            max_accuracy_meters=eff_max_acc,
            reason_code="OUTSIDE_GEOFENCE",
            reason_message="La geocerca radial no tiene centro o radio válido configurado.",
        )

    distance = round(haversine_distance_meters(latitude, longitude, center_lat, center_lon), 2)
    if distance > radius_meters:
        return GeofenceEvaluationResult(
            inside=False,
            accuracy_valid=True,
            distance_meters=distance,
            max_accuracy_meters=eff_max_acc,
            reason_code="OUTSIDE_GEOFENCE",
            reason_message=(
                f"Estás a {distance:.1f} m del punto autorizado (radio permitido: {radius_meters:.1f} m)."
            ),
        )

    return GeofenceEvaluationResult(
        inside=True,
        accuracy_valid=True,
        distance_meters=distance,
        max_accuracy_meters=eff_max_acc,
    )
