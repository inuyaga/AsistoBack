"""Cliente HTTP hacia el microservicio sidecar .NET 8 (AsistoIsup) y adaptador HikvisionISUPClient.

HikvisionISUPClient hereda de HikvisionISAPIClient y sobrescribe únicamente la capa de transporte
(_request), permitiendo reutilizar todos los servicios de dominio existentes (probe, usuarios,
rostros, eventos AcsEvent, reloj, puerta, reinicio, notificaciones HTTP) a través de ISUP 5.0
sin VPN ni IP pública en los biométricos.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import httpx

from app.core.config import settings
from app.core.isapi import (
    HikvisionISAPIClient,
    ISAPIError,
    ImageNotSuport,
    SubpicAnalysisModelingError,
)
from app.core.isup_face_staging import stage_isup_face_image


class IsupSidecarClient:
    """Cliente de bajo nivel para comunicarse con la API interna del sidecar .NET 8 (AsistoIsup)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        header_name: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        raw_url = (base_url if base_url is not None else settings.ISUP_SIDECAR_URL) or ""
        self.base_url = raw_url.strip().rstrip("/")
        self.api_key = (api_key if api_key is not None else settings.ISUP_INTERNAL_API_KEY).strip()
        self.header_name = (
            header_name if header_name is not None else settings.ISUP_INTERNAL_API_HEADER
        ).strip() or "X-Internal-Api-Key"
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None else settings.ISUP_REQUEST_TIMEOUT_SECONDS
        )

    def _ensure_configured(self) -> None:
        if not self.base_url:
            raise ISAPIError(
                "ISUP_SIDECAR_URL no está configurado en el backend. Define la URL interna del sidecar .NET 8 (ej. http://asisto-isup:8080).",
                status_code=503,
            )
        if not self.api_key:
            raise ISAPIError(
                "ISUP_INTERNAL_API_KEY no está configurada en el backend.",
                status_code=500,
            )

    def _headers(self) -> dict[str, str]:
        return {
            self.header_name: self.api_key,
            "User-Agent": "AsistoBack-ISUP-Client/1.0",
        }

    def health(self, timeout_override: Optional[float] = None) -> dict[str, Any]:
        """Consulta GET /api/health del sidecar ISUP."""
        self._ensure_configured()
        eff_timeout = timeout_override or min(self.timeout_seconds, 8.0)
        try:
            with httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(eff_timeout)) as client:
                resp = client.get("/api/health", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ISAPIError(
                f"No se pudo conectar con el sidecar ISUP en {self.base_url}: {exc}",
                status_code=503,
            ) from exc

        if resp.status_code == 401:
            raise ISAPIError(
                "Autenticación rechazada por el sidecar ISUP (verifica ISUP_INTERNAL_API_KEY).",
                status_code=502,
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ISAPIError(
                f"Respuesta inválida en /api/health del sidecar ISUP (HTTP {resp.status_code}).",
                status_code=502,
            ) from exc

        if isinstance(data, dict):
            data.setdefault("http_status", resp.status_code)
            return data
        return {"status": "unknown", "http_status": resp.status_code}

    def list_devices(
        self,
        *,
        online_only: bool = False,
        timeout_override: Optional[float] = None,
    ) -> dict[str, Any]:
        """Consulta GET /api/devices del sidecar ISUP."""
        self._ensure_configured()
        eff_timeout = timeout_override or min(self.timeout_seconds, 8.0)
        try:
            with httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(eff_timeout)) as client:
                resp = client.get(
                    "/api/devices",
                    params={"onlineOnly": str(online_only).lower()},
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ISAPIError(
                f"No se pudo consultar dispositivos en el sidecar ISUP ({self.base_url}): {exc}",
                status_code=503,
            ) from exc

        if resp.status_code == 401:
            raise ISAPIError(
                "Autenticación rechazada por el sidecar ISUP (verifica ISUP_INTERNAL_API_KEY).",
                status_code=502,
            )
        if resp.status_code >= 400:
            raise ISAPIError(
                f"Error consultando /api/devices en sidecar ISUP (HTTP {resp.status_code}): {resp.text[:300]}",
                status_code=502,
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise ISAPIError(
                "El sidecar ISUP no devolvió JSON válido en /api/devices.",
                status_code=502,
            ) from exc

        return payload if isinstance(payload, dict) else {"count": 0, "onlineCount": 0, "devices": []}

    def create_user_and_face(
        self,
        isup_device_id: str,
        *,
        employee_no: str,
        name: str,
        user_type: str = "normal",
        valid_begin_time: Optional[str] = None,
        valid_end_time: Optional[str] = None,
        upsert_user: bool = True,
        face_image_base64: Optional[str] = None,
        use_face_setup: bool = True,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        """Invoca el endpoint compuesto POST /api/devices/{deviceId}/users del sidecar ISUP."""
        self._ensure_configured()
        eff_timeout = float(timeout_seconds or self.timeout_seconds)
        payload: dict[str, Any] = {
            "employeeNo": str(employee_no).strip(),
            "name": name.strip(),
            "userType": user_type,
            "upsertUser": upsert_user,
            "useFaceSetUp": use_face_setup,
            "timeoutMs": int(eff_timeout * 1000),
        }
        if valid_begin_time:
            payload["validBeginTime"] = valid_begin_time
        if valid_end_time:
            payload["validEndTime"] = valid_end_time
        if face_image_base64:
            payload["faceImageBase64"] = face_image_base64

        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(eff_timeout + 5.0),
            ) as client:
                resp = client.post(
                    f"/api/devices/{isup_device_id}/users",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ISAPIError(
                f"Timeout esperando respuesta del sidecar ISUP para '{isup_device_id}' ({eff_timeout}s).",
                status_code=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise ISAPIError(
                f"Error de conexión con el sidecar ISUP ({self.base_url}): {exc}",
                status_code=503,
            ) from exc

        try:
            data = resp.json()
        except Exception as exc:
            raise ISAPIError(
                f"Respuesta no JSON desde /api/devices/{isup_device_id}/users (HTTP {resp.status_code}).",
                status_code=502,
            ) from exc

        if resp.status_code >= 400 and not isinstance(data, dict):
            raise ISAPIError(
                f"Error en sidecar ISUP (HTTP {resp.status_code}): {resp.text[:300]}",
                status_code=resp.status_code,
            )
        return data

    def passthrough(
        self,
        isup_device_id: str,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        xml_body: Optional[str] = None,
        files: Optional[dict[str, Any]] = None,
        raw_bytes: Optional[bytes] = None,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        """Ejecuta un comando ISAPI Passthrough genérico mediante POST /api/devices/{deviceId}/isapi."""
        self._ensure_configured()
        eff_timeout = float(timeout_seconds or self.timeout_seconds)
        timeout_ms = max(2000, int(eff_timeout * 1000))
        endpoint = f"/api/devices/{isup_device_id}/isapi"

        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(eff_timeout + 5.0),
            ) as client:
                if files:
                    # En modo multipart enviamos method/path/timeoutMs + campos/archivos al sidecar
                    form_data: dict[str, str] = {
                        "method": method.upper(),
                        "path": path,
                        "timeoutMs": str(timeout_ms),
                    }
                    multipart_files: dict[str, Any] = {}

                    for field_name, spec in files.items():
                        if isinstance(spec, tuple) and len(spec) >= 2:
                            filename = spec[0]
                            content = spec[1]
                            content_type = spec[2] if len(spec) >= 3 else "application/octet-stream"
                            if filename is None:
                                form_data[field_name] = (
                                    content.decode("utf-8", errors="ignore")
                                    if isinstance(content, (bytes, bytearray))
                                    else str(content)
                                )
                            else:
                                multipart_files[field_name] = (filename, content, content_type)
                        else:
                            multipart_files[field_name] = spec

                    resp = client.post(
                        endpoint,
                        headers=self._headers(),
                        data=form_data,
                        files=multipart_files or None,
                    )
                else:
                    body_payload: dict[str, Any] = {
                        "method": method.upper(),
                        "path": path,
                        "timeoutMs": timeout_ms,
                    }
                    if raw_bytes is not None:
                        body_payload["bodyBase64"] = base64.b64encode(raw_bytes).decode("ascii")
                    elif json_body is not None:
                        body_payload["body"] = json_body
                    elif xml_body is not None:
                        body_payload["body"] = xml_body

                    resp = client.post(
                        endpoint,
                        headers=self._headers(),
                        json=body_payload,
                    )
        except httpx.TimeoutException as exc:
            raise ISAPIError(
                f"Tiempo de espera agotado en passthrough ISUP para '{isup_device_id}' ({eff_timeout}s).",
                status_code=504,
            ) from exc
        except httpx.ConnectError as exc:
            raise ISAPIError(
                f"No se pudo conectar con el sidecar ISUP en {self.base_url}. Verifica que el contenedor asisto-isup esté activo.",
                status_code=503,
            ) from exc
        except httpx.HTTPError as exc:
            raise ISAPIError(
                f"Error de transporte hacia el sidecar ISUP ({self.base_url}): {exc}",
                status_code=502,
            ) from exc

        if resp.status_code == 401:
            raise ISAPIError(
                "Autenticación rechazada por el sidecar ISUP (revisa ISUP_INTERNAL_API_KEY).",
                status_code=502,
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ISAPIError(
                f"Respuesta inválida del sidecar ISUP (HTTP {resp.status_code}): {resp.text[:300]}",
                status_code=502,
            ) from exc

        if not isinstance(data, dict):
            raise ISAPIError(
                f"Formato inesperado en respuesta del sidecar ISUP (HTTP {resp.status_code}).",
                status_code=502,
            )

        return data


class HikvisionISUPClient(HikvisionISAPIClient):
    """Adaptador drop-in de HikvisionISAPIClient que enruta todos los comandos ISAPI vía ISUP 5.0."""

    def __init__(
        self,
        isup_device_id: str,
        *,
        timeout_seconds: int = 10,
        sidecar_client: Optional[IsupSidecarClient] = None,
    ) -> None:
        cleaned_id = (isup_device_id or "").strip()
        if not cleaned_id:
            raise ISAPIError(
                "El dispositivo está en modo ISUP pero no tiene configurado 'isup_device_id'.",
                status_code=400,
            )

        super().__init__(
            ip_address=cleaned_id,
            port=7660,
            username="isup",
            password="",
            protocol="http",
            verify_ssl=False,
            timeout_seconds=max(2, min(timeout_seconds, 30)),
        )
        self.isup_device_id = cleaned_id
        self.base_url = f"isup://{cleaned_id}"
        self.sidecar = sidecar_client or IsupSidecarClient(timeout_seconds=float(self.timeout_seconds))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        xml_body: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
        timeout_override: Optional[float] = None,
    ) -> httpx.Response:
        normalized_path = path if path.startswith("/") else f"/{path}"
        if params:
            sep = "&" if "?" in normalized_path else "?"
            normalized_path = f"{normalized_path}{sep}{urlencode(params, doseq=True)}"

        # Si se pasa multipart de rostro desde save_face_image, Hikvision ISUP espera el part 'FaceImage'
        mapped_files = files
        if files and "FaceDataRecord" in files and "img" in files and "FaceImage" not in files:
            mapped_files = {
                ("FaceImage" if k == "img" else k): v
                for k, v in files.items()
            }

        effective_timeout = float(timeout_override or self.timeout_seconds)

        res = self.sidecar.passthrough(
            self.isup_device_id,
            method=method.upper(),
            path=normalized_path,
            json_body=json_body,
            xml_body=xml_body,
            files=mapped_files,
            timeout_seconds=effective_timeout,
        )

        if not res.get("success", False):
            sdk_code = res.get("sdkErrorCode")
            sdk_name = res.get("sdkErrorName") or "ISUP_ERROR"
            err_detail = res.get("errorDetail") or res.get("rawResponse") or f"Error ISUP ({sdk_code}: {sdk_name})"
            http_status = int(res.get("httpStatusCode") or 502)
            # Mapear 503/404 del sidecar a 502/504 para que las rutas marquen el equipo como offline consistentemente
            mapped_status = 502 if http_status in (404, 503) else http_status
            raise ISAPIError(
                f"[ISUP {self.isup_device_id}] {err_detail}",
                status_code=mapped_status,
            )

        raw_b64 = res.get("rawResponseBase64")
        if isinstance(raw_b64, str) and raw_b64:
            content_bytes = base64.b64decode(raw_b64)
            content_type = (
                "image/jpeg"
                if content_bytes.startswith(b"\xff\xd8\xff")
                else "application/octet-stream"
            )
        else:
            raw_text = str(res.get("rawResponse") or "")
            content_bytes = raw_text.encode("utf-8")
            trimmed = raw_text.lstrip()
            content_type = (
                "application/json; charset=utf-8"
                if trimmed.startswith("{") or trimmed.startswith("[")
                else "application/xml; charset=utf-8"
                if trimmed.startswith("<")
                else "text/plain; charset=utf-8"
            )

        # Evaluar errores de nivel ISAPI devueltos dentro del payload (statusCode != 1)
        isapi_status_code = res.get("isapiStatusCode")
        if isapi_status_code is not None and int(isapi_status_code) != 1:
            sub = res.get("isapiSubStatusCode") or ""
            status_str = res.get("isapiStatusString") or "ISAPI Error"
            error_msg = res.get("isapiErrorMsg") or ""
            if not error_msg:
                raw_text_err = str(res.get("rawResponse") or "").lstrip()
                if raw_text_err.startswith("{"):
                    try:
                        parsed_err = json.loads(raw_text_err)
                        if isinstance(parsed_err, dict) and parsed_err.get("errorMsg"):
                            error_msg = str(parsed_err["errorMsg"]).strip()
                    except Exception:
                        pass
            detail = f"{status_str} ({sub})".strip() if sub else str(status_str)
            if error_msg:
                detail = f"{detail}: {error_msg}"
            raise ISAPIError(
                f"Respuesta de error ISAPI vía ISUP ({detail})",
                status_code=400,
            )

        dummy_request = httpx.Request(method.upper(), f"http://isup.internal{normalized_path}")
        return httpx.Response(
            status_code=200,
            headers={"content-type": content_type},
            content=content_bytes,
            request=dummy_request,
        )

    def save_face_image(self, base64_img: str, employee_no: str) -> dict[str, Any]:
        """Registra un rostro JPEG en modo ISUP 5.0 enviando JSON puro con `faceURL` a FDSetUp.

        En ISUP 5.0, NET_ECMS_ISAPIPassThrough no transmite cabeceras HTTP (`Content-Type: multipart/form-data`),
        por lo que el biométrico rechaza payloads multipart con `MessageParametersLack`.
        En su lugar, se publica temporalmente el JPEG en una URL corta y se envía JSON con `faceURL`
        para que el reloj descargue la fotografía directamente vía HTTP y modele el rostro.
        """
        raw_str = (base64_img or "").strip()
        if not raw_str:
            raise ImageNotSuport("No se proporcionó ninguna imagen en base64")

        if "," in raw_str and raw_str.startswith("data:"):
            header, b64_data = raw_str.split(",", 1)
            if "image/jpeg" not in header.lower() and "image/jpg" not in header.lower():
                raise ImageNotSuport(
                    "Formato de imagen no soportado. El dispositivo Hikvision requiere imagen JPEG (data:image/jpeg;base64,...)."
                )
        else:
            b64_data = raw_str

        try:
            image_bytes = base64.b64decode(b64_data, validate=False)
        except Exception as exc:
            raise ImageNotSuport("La cadena base64 de la imagen facial es inválida") from exc

        if len(image_bytes) < 4 or not image_bytes.startswith(b"\xff\xd8\xff"):
            raise ImageNotSuport(
                "El archivo decodificado no es un JPEG válido. Usa una fotografía en formato JPG/JPEG."
            )

        clean_emp_no = str(employee_no).strip()
        _, face_url = stage_isup_face_image(image_bytes)

        payload = {
            "faceLibType": "blackFD",
            "FDID": "1",
            "FPID": clean_emp_no,
            "faceURL": face_url,
        }

        try:
            resp = self._request(
                "PUT",
                "/ISAPI/Intelligent/FDLib/FDSetUp",
                params={"format": "json"},
                json_body=payload,
                timeout_override=20.0,
            )
        except ISAPIError as exc:
            lower_msg = exc.message.lower()
            if "modeling" in lower_msg or "subpic" in lower_msg:
                raise SubpicAnalysisModelingError(
                    f"El reloj rechazó la fotografía porque no pudo modelar el rostro: {exc.message}",
                    exc.status_code,
                ) from exc
            if any(k in lower_msg for k in ("faceurl", "download", "connect", "url", "timeout")):
                raise ISAPIError(
                    f"{exc.message} (El reloj intentó descargar la foto desde {face_url}. "
                    "Verifica que el biométrico tenga acceso HTTP a esa URL/puerto o configura ISUP_FACE_PUBLIC_BASE_URL).",
                    exc.status_code,
                ) from exc
            raise

        try:
            body = resp.json()
            if isinstance(body, dict):
                sub_code = str(body.get("subStatusCode") or "")
                if body.get("statusCode", 1) != 1 and sub_code not in ("", "ok"):
                    raise SubpicAnalysisModelingError(
                        f"El dispositivo no pudo modelar el rostro ({sub_code}): {body.get('errorMsg') or body.get('statusString')}"
                    )
        except SubpicAnalysisModelingError:
            raise
        except Exception:
            pass

        return {
            "employee_no": clean_emp_no,
            "status": "OK",
            "face_url": face_url,
            "message": f"Rostro JPEG registrado correctamente vía ISUP (faceURL) para el empleado {clean_emp_no}",
        }

    def _get_image_file_safe(self, file_url: str) -> bytes:
        """En modo ISUP, extrae solo path+query de la URL interna del equipo y la solicita vía passthrough."""
        parsed = urlparse(file_url)
        path_and_query = parsed.path or file_url
        if parsed.query:
            path_and_query += f"?{parsed.query}"
        resp = self._request("GET", path_and_query, timeout_override=15.0)
        return resp.content

